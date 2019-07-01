# -*- coding: utf-8 -*-
"""
Created on Wed Mar 14 13:06:19 2018

@author: Okada
"""

import boto3
import os
import shutil
import multiprocessing
import string
import random
import datetime
import time
import json
import ecsub.aws
import ecsub.aws_config
import ecsub.tools
import ecsub.metrics

def read_tasksfile(tasks_file, cluster_name):
    
    tasks = []
    header = []

    for line in open(tasks_file).readlines():
        text = line.rstrip("\r\n")
        if len(text.rstrip()) == 0:
            continue
        if header == []:
            for item in text.split("\t"):
                v = item.strip(" ").split(" ")
                if v[0] == "":
                    header.append({"type": "", "recursive": False, "name": ""})
                
                elif v[0].lower() == "--env":
                    header.append({"type": "env", "recursive": False, "name": v[-1]})
                elif v[0].lower() == "--input-recursive":
                    header.append({"type": "input", "recursive": True, "name": v[-1]})
                elif v[0].lower() == "--input":
                    header.append({"type": "input", "recursive": False, "name": v[-1]})
                elif v[0].lower() == "--output-recursive":
                    header.append({"type": "output", "recursive": True, "name": v[-1]})
                elif v[0].lower() == "--output":
                    header.append({"type": "output", "recursive": False, "name": v[-1]})
                else:
                    print (ecsub.tools.error_message (cluster_name, None, "type %s is not support." % (v[0])))
                    return None
            continue
        
        items = text.split("\t")
        for i in range(len(items)):
            if header[i]["type"] in ["input", "output"]:
                if items[i] == "":
                    continue
                if not items[i].startswith("s3://"):
                    print (ecsub.tools.error_message(cluster_name, None, "'%s' is invalid S3 path." % (items[i])))
                    return None
            
        tasks.append(items)

    return {"tasks": tasks, "header": header}


def write_runsh(task_params, runsh, shell, is_request_payer):
   
    run_template = """set -ex
pwd

SCRIPT_SETENV_NAME=`basename ${{SCRIPT_SETENV_PATH}}`
SCRIPT_RUN_NAME=`basename ${{SCRIPT_RUN_PATH}}`
SCRIPT_DOWNLOADER_NAME=`basename ${{SCRIPT_DOWNLOADER_PATH}}`
SCRIPT_UPLOADER_NAME=`basename ${{SCRIPT_UPLOADER_PATH}}`

aws s3 cp {option} ${{SCRIPT_SETENV_PATH}} ${{SCRIPT_SETENV_NAME}} --only-show-errors
aws s3 cp {option} ${{SCRIPT_RUN_PATH}} ${{SCRIPT_RUN_NAME}} --only-show-errors
aws s3 cp {option} ${{SCRIPT_DOWNLOADER_PATH}} ${{SCRIPT_DOWNLOADER_NAME}} --only-show-errors
aws s3 cp {option} ${{SCRIPT_UPLOADER_PATH}} ${{SCRIPT_UPLOADER_NAME}} --only-show-errors

source ${{SCRIPT_SETENV_NAME}}
df -h

{shell} ${{SCRIPT_DOWNLOADER_NAME}}

# run main script
{shell} ${{SCRIPT_RUN_NAME}}

#if [ $? -gt 0 ]; then exit $?; fi

# upload
{shell} ${{SCRIPT_UPLOADER_NAME}}
"""
    option = ""
    if is_request_payer:
        option = "--request-payer requester"
        
    open(runsh, "w").write(run_template.format(
        shell = shell,
        option = option
    ))
    
def write_s3_scripts(task_params, payer_buckets, setenv, downloader, uploader, no):
   
    env_text = "set -x\n"
    dw_text = "set -x\n"
    up_text = "set -x\n"
    
    for i in range(len(task_params["tasks"][no])):
        
        if task_params["header"][i]["type"] == "env":
            env_text += 'export %s="%s"\n' % (task_params["header"][i]["name"], task_params["tasks"][no][i])
            continue
            
        s3_path = task_params["tasks"][no][i]
        scratch_path = task_params["tasks"][no][i].replace("s3://", "/scratch/AWS_DATA/")
    
        env_text += 'export S3_%s="%s"\n' % (task_params["header"][i]["name"], s3_path)
        env_text += 'export %s="%s"\n' % (task_params["header"][i]["name"], scratch_path)
    
        if s3_path == "":
            continue
        
        cmd_template = 'aws s3 cp --only-show-errors {option} {path1} {path2}\n'
        
        option = []
        if task_params["header"][i]["recursive"]:
            option.append("--recursive")
        
        if ecsub.tools.is_request_payer_bucket(s3_path, payer_buckets):
            option.append("--request-payer requester")
        
        if task_params["header"][i]["type"] == "input":
            dw_text += cmd_template.format(option = " ".join(option), path1 = s3_path, path2 = scratch_path)
            
        elif task_params["header"][i]["type"] == "output":
            up_text += cmd_template.format(option = " ".join(option), path1 = scratch_path, path2 = s3_path)

    open(setenv, "w").write(env_text)
    open(downloader, "w").write(dw_text)
    open(uploader, "w").write(up_text)
    
def check_inputfiles_collect(files, dirs, cluster_name):
    
    uncheck_dirs = []
    uncheck_dirs.extend(dirs)
    for d in dirs:
        for f in files:
            if f.startswith(d):
                uncheck_dirs.remove(d)
                break

    tree = {}
    for path in files:
        bucket = path.split("/")[0]
        if not bucket in tree:
            tree[bucket] = {}
            tree[bucket]["files"] = []
            tree[bucket]["dirs"] = []
        tree[bucket]["files"].append(path.replace(bucket + "/", "", 1))

    for path in uncheck_dirs:
        bucket = path.split("/")[0]
        if not bucket in tree:
            tree[bucket] = {}
            tree[bucket]["files"] = []
            tree[bucket]["dirs"] = []
        tree[bucket]["dirs"].append(path.replace(bucket + "/", "", 1))
    
    s3 = boto3.resource('s3')
    for key in tree:
        bucket = s3.Bucket(key)
        print (ecsub.tools.info_message (cluster_name, None, "checking s3 bucket '%s'..." % (key)))
        for obj in bucket.objects.all():
            if obj.key in tree[key]["files"]:
                tree[key]["files"].remove(obj.key)

            match = [s for s in tree[key]["dirs"] if obj.key.startswith(s)]
            for d in match:
                tree[key]["dirs"].remove(d)
            if len(tree[key]["files"]) == 0 and len(tree[key]["dirs"]) == 0:
                break

    result = []
    for key in tree:
        for typ in tree[key]:
            for path in tree[key][typ]:
                result.append("%s/%s" % (key, path))
    
    return result
    
def check_inputfiles_partial(aws_instance, files, dirs):
        
    for path in files + dirs:
        if not aws_instance.check_file(path):
            return [path]

    return []

def check_bucket_location(pathes):
    buckets = []
    for p in pathes:
        path = p.replace("s3://", "", 1).strip("/").rstrip("/").split("/")[0]
        if path == "":
            continue
        buckets.append(path)
    
    client = boto3.client("s3")
    regions = []
    for bucket in sorted(list(set(buckets))):
        response = client.get_bucket_location(Bucket=bucket)
        if response['LocationConstraint'] == None:
            print (ecsub.tools.warning_message (None, None, "Failue get_bucket_location '%s'..." % (bucket)))
        else:
            regions.append(response['LocationConstraint'])
    
    current_session = boto3.session.Session()
    regions.append(current_session.region_name)
    regions = sorted(list(set(regions)))
    
    return regions

def check_inputfiles(aws_instance, task_params, cluster_name, payer_buckets, work_bucket):
    
    files = []
    dirs = []
    files_rp = []
    dirs_rp = []
    outputs = []
    for task in task_params["tasks"]:
        for i in range(len(task)):
            if task_params["header"][i]["type"] == "output":
                outputs.append(task[i])
                
            if task_params["header"][i]["type"] != "input":
                continue
            
            path = task[i].replace("s3://", "", 1).strip("/").rstrip("/")
            if path == "":
                continue
            
            if ecsub.tools.is_request_payer_bucket(task[i], payer_buckets):
                if task_params["header"][i]["recursive"]:
                    dirs_rp.append(path)
                else:
                    files_rp.append(path)
            else:
                if task_params["header"][i]["recursive"]:
                    dirs.append(path)
                else:
                    files.append(path)
    
    regions = check_bucket_location(dirs + files + [work_bucket] + outputs)
          
    invalid_files = []
    invalid_files += check_inputfiles_collect(sorted(list(set(files))), sorted(list(set(dirs))), cluster_name)
    invalid_files += check_inputfiles_partial(aws_instance, sorted(list(set(files_rp))), sorted(list(set(dirs_rp))))
    
    return (regions, invalid_files)

def upload_scripts(task_params, aws_instance, local_root, s3_root, script, cluster_name, shell, request_payer):

    runsh = local_root + "/run.sh"
    s3_runsh = s3_root + "/run.sh"
    write_runsh(task_params, runsh, shell, ecsub.tools.is_request_payer_bucket(s3_root, request_payer))
    
    s3_setenv_list = []
    s3_downloader_list = []
    s3_uploader_list = []
    for i in range(len(task_params["tasks"])):
        setenv = local_root + "/setenv.%d.sh" % (i)
        s3_setenv = s3_root + "/setenv.%d.sh" % (i)
        downloader = local_root + "/downloader.%d.sh" % (i)
        s3_downloader = s3_root + "/downloader.%d.sh" % (i)
        uploader = local_root + "/uploader.%d.sh" % (i)
        s3_uploader = s3_root + "/uploader.%d.sh" % (i)
        
        write_s3_scripts(task_params, request_payer, setenv, downloader, uploader, i)
        s3_setenv_list.append(s3_setenv)
        s3_downloader_list.append(s3_downloader)
        s3_uploader_list.append(s3_uploader)
        
    aws_instance.s3_copy(local_root, s3_root, True)
    
    s3_script = s3_root + "/userdata/" + os.path.basename(script)
    aws_instance.s3_copy(script, s3_script, False)
    
    pathes = []
    for p in [s3_runsh, s3_script] + s3_setenv_list + s3_downloader_list + s3_uploader_list:
        pathes.append(p.replace("s3://", "", 1).strip("/").rstrip("/"))
        
    invalid_files = check_inputfiles_collect(pathes, [], cluster_name)
    #invalid_files = check_inputfiles_partial(aws_instance, [s3_runsh, s3_script] + s3_setenv_list + s3_downloader_list + s3_uploader_list, [])
    if len(invalid_files) > 0:
        return False
    
    aws_instance.set_s3files(s3_runsh, s3_script, s3_setenv_list, s3_downloader_list, s3_uploader_list)
    
    return True

def _run_task(aws_instance, no, instance_id):
    
    system_error = False
    exit_code = 1
    task_log = None
    
    try:
        (exit_code, task_log) = aws_instance.run_task(no, instance_id)
        if exit_code == 127:
            system_error = True
    
    except Exception as e:
        print (ecsub.tools.error_message (aws_instance.cluster_name, no, e))
    
    if aws_instance.flyaway and exit_code == 0:
        return (exit_code, task_log, system_error)
        
    aws_instance.terminate_instances(instance_id, no)
    
    return (exit_code, task_log, system_error)

def submit_task_ondemand(aws_instance, no):
    
    exit_code = 1
    task_log = None
    
    if not aws_instance.set_ondemand_price(no):
        return (exit_code, task_log)
    
    for i in range(3):
        instance_id = aws_instance.run_instances_ondemand (no)
        if instance_id == None:
            break
        
        (exit_code, task_log, system_error) = _run_task(aws_instance, no, instance_id)
            
        if system_error:
            continue
        else:
            return (exit_code, task_log)
        
    return (exit_code, task_log)

def submit_task_spot(aws_instance, no):

    exit_code = 1
    task_log = None
    
    for itype in aws_instance.aws_ec2_instance_type_list:
        
        aws_instance.task_param[no]["aws_ec2_instance_type"] = itype
        
        if not aws_instance.set_ondemand_price(no):
            continue
        if not aws_instance.set_spot_price(no):
            continue
        
        for i in range(3):
            instance_id = aws_instance.run_instances_spot (no)
            if instance_id == None:
                break

            (exit_code, task_log, system_error) = _run_task(aws_instance, no, instance_id)
            if aws_instance.flyaway and exit_code == 0:
                break
                
            aws_instance.cancel_spot_instance_requests (no = no, instance_id = instance_id)
                
            if system_error:
                continue
            elif exit_code == -1:
                break
            else:
                return (exit_code, task_log, False)
    
    return (exit_code, task_log, True)

def _hour_delta(start_t, end_t):
    return (end_t - start_t).total_seconds()/3600.0

def _set_job_info(task_param, start_t, end_t, task_log, exit_code):
    
    info = {
        "Ec2InstanceType": task_param["aws_ec2_instance_type"],
        "End": end_t,
        "ExitCode": exit_code,
        "LogLocal": task_log, 
        "OdPrice": task_param["od_price"],
        "Start": start_t,
        "Spot": task_param["spot"],
        "SpotAz": task_param["spot_az"],
        "SpotPrice": task_param["spot_price"],
        "WorkHours": _hour_delta(start_t, end_t),
        "InstanceId": "",
        "SubnetId": "",
        "Memory": 0,
        "vCpu": 0,
    }
    
    if task_log == None:
        return info
    
    task = json.load(open(task_log))["tasks"][0]
    info["InstanceId"] = task["instance_id"]
    info["SubnetId"] = task["subnet_id"]
    info["Memory"] = task["overrides"]["containerOverrides"][0]["memory"]
    info["vCpu"] = task["overrides"]["containerOverrides"][0]["cpu"]

    return info

def _save_summary_file(job_summary, print_cost):
    
    template = " + instance-type %s (%s) %.3f USD (%s: %.3f USD), running-time %.3f Hour"
    costs = 0.0
    items = []
    for job in job_summary["Jobs"]:
        wtime = _hour_delta(job["Start"], job["End"])
        
        if job["Spot"]:
            costs += job["SpotPrice"] * wtime
            items.append(template % (job["Ec2InstanceType"], "spot", job["SpotPrice"], "od", job["OdPrice"], wtime))
        else:
            costs += job["OdPrice"] * wtime
            items.append(template % (job["Ec2InstanceType"], "ondemand", job["OdPrice"], "spot", job["SpotPrice"], wtime))            
        
        job["Start"] = ecsub.tools.datetime_to_standardformat(job["Start"])
        job["End"] = ecsub.tools.datetime_to_standardformat(job["End"])

    if print_cost:        
        message = "The cost of this job is %.3f USD. \n%s" % (costs, "\n".join(items))
        print (ecsub.tools.info_message (job_summary["ClusterName"], job_summary["No"], message))
    
    log_file = "%s/log/summary.%03d.log" % (job_summary["Wdir"], job_summary["No"]) 
    json.dump(job_summary, open(log_file, "w"), indent=4, separators=(',', ': '), sort_keys=True)
    
def submit_task(aws_instance, no, task_params, spot):
    
    job_summary = {
        "AccountId": aws_instance.aws_accountid,
        "AmiId": aws_instance.aws_ami_id,
        "AutoKey": aws_instance.aws_key_auto,
        "ClusterName": aws_instance.cluster_name,
        "ClusterArn": aws_instance.cluster_arn,
        "Ec2InstanceDiskSize": aws_instance.aws_ec2_instance_disk_size,
        "End": None,
        "Image": aws_instance.image,
        "KeyName": aws_instance.aws_key_name,
        "LogGroupName": aws_instance.log_group_name,
        "No": no,
        "Region": aws_instance.aws_region,
        "RequestPayerBucket": aws_instance.request_payer,
        "S3RunSh": aws_instance.s3_runsh,
        "S3Script": aws_instance.s3_script,
        "S3Setenv": aws_instance.s3_setenv[no],
        "SecurityGroupId": aws_instance.aws_security_group_id,
        "Shell": aws_instance.shell,
        "Spot": aws_instance.spot,
        "Start": ecsub.tools.datetime_to_standardformat(datetime.datetime.now()),
        "TaskDefinitionAn": aws_instance.task_definition_arn,
        "UseAmazonEcr": aws_instance.use_amazon_ecr,
        "Wdir": aws_instance.wdir,
        "Jobs":[]
    }
    if aws_instance.flyaway == False:
        _save_summary_file(job_summary, False)

    if spot:
        start_t = datetime.datetime.now()
        (exit_code, task_log, retry) = submit_task_spot(aws_instance, no)
        job_summary["Jobs"].append(_set_job_info(
            aws_instance.task_param[no], start_t, datetime.datetime.now(), task_log, exit_code
        ))
        
        if aws_instance.retry_od and retry:
            start_t = datetime.datetime.now()
            aws_instance.task_param[no]["aws_ec2_instance_type"] = aws_instance.aws_ec2_instance_type_list[0]
            (exit_code, task_log) = submit_task_ondemand(aws_instance, no)
            job_summary["Jobs"].append(_set_job_info(
                aws_instance.task_param[no], start_t, datetime.datetime.now(), task_log, exit_code
            ))
    else:
        start_t = datetime.datetime.now()
        (exit_code, task_log) = submit_task_ondemand(aws_instance, no)
        job_summary["Jobs"].append(_set_job_info(
            aws_instance.task_param[no], start_t, datetime.datetime.now(), task_log, exit_code
        ))
    
    job_summary["SubnetId"] = aws_instance.aws_subnet_id
    job_summary["End"] = ecsub.tools.datetime_to_standardformat(datetime.datetime.now())
    
    if aws_instance.flyaway == False:
        ecsub.metrics.entry_point(aws_instance.wdir, no)
        _save_summary_file(job_summary, True)
       
    exit (exit_code)
    
def main(params):

    # set cluster_name
    params["cluster_name"] = params["task_name"]
    if params["cluster_name"] == "":
        params["cluster_name"] = os.path.splitext(os.path.basename(params["tasks"]))[0] \
            + '-' \
            + ''.join([random.choice(string.ascii_letters + string.digits) for i in range(5)])
            
    # check param
    instance_type_list = params["aws_ec2_instance_type_list"].replace(" ", "")
    if len(instance_type_list) == 0:
        params["aws_ec2_instance_type_list"] = [params["aws_ec2_instance_type"]]
    else:
        params["aws_ec2_instance_type_list"] = instance_type_list.split(",")
        
    if params["aws_ec2_instance_type"] != "":
        pass
            
    elif len(params["aws_ec2_instance_type_list"]) > 0:
        if not params["spot"]:
            print (ecsub.tools.error_message (params["cluster_name"], None, "--aws-ec2-instance-type-list option is not support with ondemand-instance mode."))
            return 1
        
    else:
        print (ecsub.tools.error_message (params["cluster_name"], None, "One of --aws-ec2-instance-type option and --aws-ec2-instance-type-list option is required."))
        return 1
    
    # "request_payer": 
    request_payer = params["request_payer"].replace(" ", "")
    if len(request_payer) == 0:
        params["request_payer"] = []
    else:
        params["request_payer"] = request_payer.split(",")
        
    # read tasks file
    task_params = read_tasksfile(params["tasks"], params["cluster_name"])
    if task_params == None:
        #print (ecsub.tools.error_message (params["cluster_name"], None, "task file is invalid."))
        return 1
    
    if task_params["tasks"] == []:
        print (ecsub.tools.info_message (params["cluster_name"], None, "task file is empty."))
        return 0
    
    subdir = params["cluster_name"]
    
    params["wdir"] = params["wdir"].rstrip("/") + "/" + subdir
    params["aws_s3_bucket"] = params["aws_s3_bucket"].rstrip("/") + "/" + subdir
    
    if os.path.exists (params["wdir"]):
        shutil.rmtree(params["wdir"])
        print (ecsub.tools.info_message (params["cluster_name"], None, "'%s' existing directory was deleted." % (params["wdir"])))
        
    os.makedirs(params["wdir"])
    os.makedirs(params["wdir"] + "/log")
    os.makedirs(params["wdir"] + "/conf")
    os.makedirs(params["wdir"] + "/script")

    aws_instance = ecsub.aws.Aws_ecsub_control(params, len(task_params["tasks"]))
    
    # check task-param
    if not aws_instance.check_awsconfigure():
        return 1

    # check s3-files path
    (regions, invalid_pathes) = check_inputfiles(aws_instance, task_params, params["cluster_name"], params["request_payer"], params["aws_s3_bucket"])
    if len(regions) > 1:
        if params["ignore_location"]:
            print (ecsub.tools.warning_message (params["cluster_name"], None, "your task uses multipule regions '%s'." % (",".join(regions))))
        else:
            print (ecsub.tools.error_message (params["cluster_name"], None, "your task uses multipule regions '%s'." % (",".join(regions))))
            return 1
        
    for r in invalid_pathes:
        print (ecsub.tools.error_message (params["cluster_name"], None, "input '%s' is not access." % (r)))
    if len(invalid_pathes)> 0:
        return 1
    
    # write task-scripts, and upload to S3
    local_script_dir = params["wdir"] + "/script"
    s3_script_dir = params["aws_s3_bucket"].rstrip("/") + "/script"
    if not upload_scripts(task_params, 
                   aws_instance, 
                   local_script_dir, 
                   s3_script_dir,
                   params["script"],
                   params["cluster_name"],
                   params["shell"],
                   params["request_payer"]):
        print (ecsub.tools.error_message (params["cluster_name"], None, "failure upload files to s3 bucket: %s." % (params["aws_s3_bucket"])))
        return 1
    
    # run purocesses
    process_list = []
    
    try:
        # create-cluster
        # and register-task-definition
        if not aws_instance.create_cluster():
            aws_instance.clean_up()
            return 1
        if not aws_instance.register_task_definition():
            aws_instance.clean_up()
            return 1
        
        while len(process_list) < len(task_params["tasks"]):
            alives = 0
            for process in process_list:
                if process.exitcode == None:
                   alives += 1
                    
            jobs = params["processes"] - alives
            submitted = len(process_list)
            
            for i in range(jobs):
                no = i + submitted
                if no >= len(task_params["tasks"]):
                    break
                    
                process = multiprocessing.Process(
                        target = submit_task, 
                        name = "%s_%03d" % (params["cluster_name"], no), 
                        args = ((aws_instance, no, task_params, params["spot"]))
                )
                process.daemon == True
                process.start()
                
                process_list.append(process)
                
                time.sleep(5)
            
            time.sleep(5)
        
        exitcodes = []
        for process in process_list:
            process.join()
            if process.exitcode != None:
                exitcodes.append(process.exitcode)
        
        if params["flyaway"] == False:
            aws_instance.clean_up()
            
        # SUCCESS?
        if [0] == list(set(exitcodes)):
            return 0
        
    except Exception as e:
        print (e)
        print (ecsub.tools.important_message (params["cluster_name"], None, "Wait unti clear up the resources."))
        for process in process_list:
            process.terminate()
        print (ecsub.tools.important_message (params["cluster_name"], None, "Wait unti clear up the resources."))
        aws_instance.clean_up()
        
    except KeyboardInterrupt:
        print ("KeyboardInterrupt")
        print (ecsub.tools.important_message (params["cluster_name"], None, "Wait unti clear up the resources."))
        for process in process_list:
            process.terminate()
        print (ecsub.tools.important_message (params["cluster_name"], None, "Wait unti clear up the resources."))
        aws_instance.clean_up()
    
    return 1

def set_param(args, options):
    
    params = {
        "wdir": args.wdir,
        "image": args.image,
        "shell": args.shell,
        "use_amazon_ecr": args.use_amazon_ecr,
        "script": args.script,
        "tasks": args.tasks,
        "task_name": args.task_name,
        "aws_ec2_instance_type": args.aws_ec2_instance_type,
        "aws_ec2_instance_type_list": args.aws_ec2_instance_type_list,
        "aws_ec2_instance_disk_size": args.disk_size,
        "aws_s3_bucket": args.aws_s3_bucket,
        "aws_security_group_id": args.aws_security_group_id,
        "aws_key_name": args.aws_key_name,
        "aws_subnet_id": args.aws_subnet_id,
        "spot": args.spot,
        "retry_od": args.retry_od,
        "setx": "set -x",
        "setup_container_cmd": args.setup_container_cmd,
        "dind": args.dind,
        "processes": args.processes,
        "request_payer": args.request_payer_bucket,
        "ignore_location": args.ignore_location,
        "flyaway": False,
        "aws_account_id": args.aws_account_id,
        "aws_region": args.aws_region,
    }
    
    for op in options:
        params[op["key"]] = op["value"]
        
    return params

def entry_point(args):
    
    params = set_param(args, [])
    return main(params)
    
def entry_point_flyaway(args, arg_options = [], env_options = None):
    """
    # add proj from function call
    env_options = [
        { "name": "PROJECT_NAME", "value": "moogle"}
    ]
    """
    options = arg_options + [{"key": "flyaway", "value": True}]
    if env_options != None:
        options.append({"key": "env_options", "value": env_options})
        
    params = set_param(args, options)
    return main(params)

class Argments:
    def __init__(self):
        self.wdir = "./"
        self.image = "python:2.7.14"
        self.use_amazon_ecr = False
        self.shell = "/bin/bash"
        self.setup_container_cmd = "apt update; apt install -y python-pip; pip install awscli --upgrade; aws configure list"
        self.dind = False
        self.script = ""
        self.tasks = ""
        self.task_name = ""
        self.aws_s3_bucket = ""
        self.aws_ec2_instance_type = ""
        self.aws_ec2_instance_type_list = ""
        self.disk_size = 22
        self.processes = 20
        self.aws_security_group_id = ""
        self.aws_key_name = ""
        self.aws_subnet_id = ""
        self.spot = False
        self.retry_od = False
        self.request_payer_bucket = ""
        self.ignore_location = False
        
if __name__ == "__main__":
    pass

