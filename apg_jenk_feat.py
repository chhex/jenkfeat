#!/usr/bin/env python
import argparse
import configparser
from configparser import ConfigParser
from dataclasses import dataclass
import subprocess
import os
import shutil
from bs4 import BeautifulSoup
import re


@dataclass
class JobDetail:
    job_name: str
    module_name: str
    curr_branch: str
    local_file_name: str


def check_and_create_workdir(child, config):
    dir_ = config["CVS"]["local_work_dir"]
    target_dir = os.path.join(dir_, child)
    print(target_dir)
    if os.path.isdir(target_dir):
        shutil.rmtree(target_dir, ignore_errors=False, onerror=None)
    os.makedirs(target_dir)
    return target_dir


def get_daos_from_view(args, config):
    jobs_names = subprocess.check_output(
        ['ssh', '-l', args.user, '-p', config['JENKINS']['port'], config['JENKINS']['target_uri'],
         'list-jobs', f"%s" % config['JENKINS']['source_view']], text=True)
    exludes = config['JENKINS']['jobs_exludes'].split()
    daos = []
    for job in jobs_names.splitlines():
        name_filter = config['JENKINS']['job_endswith_filter']
        if not name_filter or job.endswith(name_filter):
            excluded = False
            for exclude in exludes:
                if exclude in job:
                    excluded = True
                    continue
            if excluded:
                continue
            daos.append(job)
    return daos


def get_and_upd_job_details(daos, args, config):
    target_dir = check_and_create_workdir("jenkins", config)
    dao_details = []
    for dao in daos:
        print(f"Gathering details of job %s" % dao)
        detail = subprocess.check_output(
            ['ssh', '-l', args.user, '-p', config['JENKINS']['port'], config['JENKINS']['target_uri'],
             'get-job', f"'%s'" % dao.replace(' ', '\\ ')], text=True)
        xml = BeautifulSoup(detail, 'xml')
        hudson_cvs_repo = xml.scm.repositories.find('hudson.scm.CvsRepository')
        repo_item = hudson_cvs_repo.repositoryItems.find('hudson.scm.CvsRepositoryItem')
        cvs_module = repo_item.modules.find('hudson.scm.CvsModule')
        source_branch = repo_item.location.locationName.contents[0]
        repo_item.location.locationName.string = config['CVS']['target_branch']
        file_name = re.sub(r"\s+", "", dao) + ".xml"
        file_path = os.path.join(target_dir, file_name)
        with open(file_path, "w") as file:
            file.write(str(xml))
        job_detail = JobDetail(job_name=dao,
                               module_name=cvs_module.remoteName.contents[0],
                               curr_branch=source_branch,
                               local_file_name=file_path)
        dao_details.append(job_detail)
    return dao_details


def co_and_branching_modules(dao_details, args, config):
    if args.is_skip_co:
        print("Skipping cvs checkout")
        return
    target_dir = check_and_create_workdir("cvs", config)
    print(f"Using work dir %s" % target_dir)
    curr_dir = os.getcwd()
    print(f"Saving current dir %s" % curr_dir)
    os.chdir(target_dir)
    print(f"Changed to dir %s" % os.getcwd())
    cvs_env = os.environ.copy()
    cvs_env["CVSROOT"] = f":ext:%s@%s:/var/local/cvs/root" % (args.user, config["CVS"]["repository"])
    cvs_env["CVS_RSH"] = "ssh"
    for module in dao_details:
        print(f"Checkout module %s" % module)
        subprocess.call(['cvs', 'co', '-r', module.curr_branch, module.module_name],
                        stdout=True, stderr=True, env=cvs_env)
        if args.is_dry_run:
            print("Not Branching module %s, , because running in dry run mode" % module)
            continue
        module_path = os.path.join(target_dir, module.module_name)
        print(module_path)
        os.chdir(module_path)
        subprocess.call(['cvs', 'tag', '-b', config["CVS"]["target_branch"]],
                        stdout=True, stderr=True, env=cvs_env)
        subprocess.call(['cvs', 'update', '-r', config["CVS"]["target_branch"]],
                        stdout=True, stderr=True, env=cvs_env)
        os.chdir(target_dir)
    os.chdir(curr_dir)


def update_module_poms(dao_details, args, config):
    if args.is_skip_pom_upd:
        print("Skippingpom.xml update of modules")
        return
    curr_dir = os.getcwd()
    dir_ = config["CVS"]["local_work_dir"]
    cvs_path = os.path.join(dir_, "cvs")
    for module in dao_details:
        module_path = os.path.join(cvs_path, module.module_name)
        pom_path = os.path.join(module_path, "pom.xml")
        print(f"Updating %s" % pom_path)
        with open(pom_path) as f:
            pom = BeautifulSoup(f, 'xml')
        parent_version = pom.project.parent.version
        print(f"Updating parent version: %s" % str(parent_version))
        parent_version.string = config["MAVEN"]["target_version"]
        print(f"Updated parent to version: %s" % str(parent_version))
        versions = pom.project.find_all('version')
        for version in versions:
            if version.contents[0].endswith("${revision}"):
                print(f"Removing module version: %s" % str(version))
                version.decompose()
        with open(pom_path, "w") as file:
            file.write(str(pom))
    os.chdir(curr_dir)


def commit_modules(dao_details, args, config):
    if args.is_dry_run or args.is_skip_commit:
        print("Skipping commit pf module changes, because running in dry run mode")
        return
    curr_dir = os.getcwd()
    dir_ = config["CVS"]["local_work_dir"]
    cvs_path = os.path.join(dir_, "cvs")
    cvs_env = os.environ.copy()
    cvs_env["CVSROOT"] = f":ext:%s@%s:/var/local/cvs/root" % (args.user, config["CVS"]["repository"])
    cvs_env["CVS_RSH"] = "ssh"
    for module in dao_details:
        module_path = os.path.join(cvs_path, module.module_name)
        os.chdir(module_path)
        subprocess.call(['cvs', 'ci', '-m', f"Updated pom.xml with new Version"], stdout=True, stderr=True, env=cvs_env)
        os.chdir(cvs_path)
    os.chdir(curr_dir)


def create_new_jobs(dao_details, args, config):
    if args.is_dry_run:
        print("Not creating new Jenkins Jobs, because running in dry run mode")
        return
    for job in dao_details:
        job_name = re.sub(r"\s+", "", job.job_name)
        job_name = job_name.replace(config['JENKINS']['source_job_name_prefix'],
                                    config['JENKINS']['target_job_name_prefix'])
        print(f"Deleting Jenkins Job %s" % job_name)
        subprocess.call(['ssh', '-l', args.user,
                         '-p', config['JENKINS']['port'],
                         config['JENKINS']['target_uri'],
                         'delete-job', job_name], stdout=True, stderr=True)
        print(f"Creating Jenkins Job %s" % job_name)
        cat = subprocess.Popen(['cat', job.local_file_name], stdout=subprocess.PIPE)
        subprocess.check_output(['ssh', '-l', args.user,
                                 '-p', config['JENKINS']['port'],
                                 config['JENKINS']['target_uri'],
                                 'create-job', job_name], stdin=cat.stdout)
        cat.wait()
        print(f"Adding Jenkins Job %s to view %s" % (job_name, config['JENKINS']['target_view']))
        subprocess.call(['ssh', '-l', args.user,
                         '-p', config['JENKINS']['port'],
                         config['JENKINS']['target_uri'],
                         'add-job-to-view', config['JENKINS']['target_view'], job_name], stdout=True, stderr=True)


def jenkins_setup():
    dsc = """This script set's up a feature branch in jenkins, according to apg conventions. 

For the configuration , see the file jenkins_config.ini

TODO more description

"""
    
    arg_parser = argparse.ArgumentParser(description=dsc,
                                           formatter_class=argparse.RawDescriptionHelpFormatter)
    arg_parser.add_argument('--user', '-u', action='store', required=True,
                            help="User, with which Jenkins jobs will be started, optional, depends on --location")
    arg_parser.add_argument('--dry', dest='is_dry_run', action='store_true',
                            help="Optional Argument, if true, the cvs changes are not commited and the jenkins jobs "
                                 "not uploaded")
    arg_parser.add_argument('--skip-co', dest='is_skip_co', action='store_true',
                            help="Optional Argument, if true, Cvs Checkout is skipped")
    arg_parser.add_argument('--skip-pom-upd', dest='is_skip_pom_upd', action='store_true',
                            help="Optional Argument, if true, the pom.xml of the modules is not updated")
    arg_parser.add_argument('--skip-commit', dest='is_skip_commit', action='store_true',
                            help="Optional Argument, if true, the pom.xml of the modules is not updated")
    args = arg_parser.parse_args()
    config: ConfigParser = configparser.ConfigParser()
    config.read("./jenkins_config.ini")
    print(f"Retrieving jobs names from source view"
          f" %s with endswith filter: %s" %
          (config['JENKINS']['source_view'],
           config['JENKINS']['job_endswith_filter']))
    daos = get_daos_from_view(args, config)
    print(f"Retrieving jobs detail from selected jobs "
          f"and updating to target branch: %s" %
          config['CVS']['target_branch'])
    dao_details = get_and_upd_job_details(daos, args, config)
    print(f"Checking out from cvs and creating target Branch %s" % config['CVS']['target_branch'])
    co_and_branching_modules(dao_details, args, config)
    print(f"Updating pom.xml of selected modules "
          f"with target Revision: %s" % config['MAVEN']['target_version'])
    update_module_poms(dao_details, args, config)
    print(f"Commiting changes to cvs to branch: %s "
          % config['CVS']['target_branch'])
    commit_modules(dao_details, args, config)
    print(f"Creating new Jenkins for Branch and Version: %s "
          % config['CVS']['target_branch'])
    create_new_jobs(dao_details, args, config)
    print(f"Done.")


if __name__ == '__main__':
    jenkins_setup()
