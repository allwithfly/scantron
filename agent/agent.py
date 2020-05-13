#!/usr/bin/env python
"""The goals of agent.py are to:

1) Utilize native Python libraries and not depend on third party or custom libraries
2) Be a single file so it can be moved, downloaded, or transferred between systems easily
"""

# Standard Python libraries.
import argparse
import datetime
import fnmatch
import json
import logging
import os
import queue
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.request

# Disable SSL/TLS verification.
# https://stackoverflow.com/questions/36600583/python-3-urllib-ignore-ssl-certificate-verification#comment96281490_36601223
ssl._create_default_https_context = ssl._create_unverified_context

ROOT_LOGGER = logging.getLogger("scantron")

# ISO8601 datetime format by default.
LOG_FORMATTER = logging.Formatter("%(asctime)s [%(threadName)-12.12s] [%(levelname)s] %(message)s")

# Track scan process IDs and subprocess.Popen() objects.
SCAN_PROCESS_DICT = {}


def get_current_time():
    """Retrieve a Django compliant pre-formated datetimestamp."""

    now_datetime = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return now_datetime


def build_masscan_command(scan_command, target_file, excluded_target_file, json_file, http_useragent):
    """Builds the masscan command."""

    # Can only have 1 file output type.
    file_options = f"-iL {target_file} -oJ {json_file} --http-user-agent {http_useragent}"

    if excluded_target_file:
        file_options += f" --excludefile {excluded_target_file}"

    # scan_command is used for both nmap and masscan commands.
    masscan_command = f"masscan {scan_command} {file_options}"

    return masscan_command


def move_wildcard_files(wildcard_filename, source_directory, destination_directory):
    """Move files with supported fnmatch patterns (* and ?)."""

    file_list = os.listdir(source_directory)

    for file_name in file_list:
        if fnmatch.fnmatch(file_name, wildcard_filename):
            shutil.move(os.path.join(source_directory, file_name), os.path.join(destination_directory, file_name))


def check_for_scan_jobs():
    """Check for new scans through the API."""

    # Build URL to pull new scan jobs.  Server determines jobs based off agent (user) making request.
    master_address = agent.config_data["master_address"]
    master_port = agent.config_data["master_port"]
    scan_agent = agent.config_data["scan_agent"]
    api_token = agent.config_data["api_token"]

    url = f"{master_address}:{master_port}/api/scheduled_scans"
    ROOT_LOGGER.info(f"check_for_scans URL: {url}")

    # Update User-Agent and add API token.
    headers = {
        "user-agent": scan_agent,
        "Authorization": f"Token {api_token}",
    }

    try:
        # Make the HTTP GET request.
        request = urllib.request.Request(method="GET", url=url, headers=headers)
        response = urllib.request.urlopen(request)

        response_code = response.status
        response_data = response.read().decode("utf-8")

        # Return response as JSON if request is successful.
        if response_code == 200:
            json_data = json.loads(response_data)
            return json_data

        else:
            ROOT_LOGGER.error(f"Could not access {master_address}:{master_port}. HTTP status code: {response_code}")
            ROOT_LOGGER.error(f"Response content: {response_data}")
            return None

    except Exception as e:
        ROOT_LOGGER.error(f"check_for_scan_jobs() function exception: {e}")


def update_scan_information(scan_job, update_info):
    """Update scan information using a PATCH API request."""

    master_address = agent.config_data["master_address"]
    master_port = agent.config_data["master_port"]
    scan_agent = agent.config_data["scan_agent"]
    api_token = agent.config_data["api_token"]
    scan_job_id = scan_job["id"]

    # Build URL to update scan job.
    url = f"{master_address}:{master_port}/api/scheduled_scans/{scan_job_id}"
    ROOT_LOGGER.info(f"update_scan_information URL: {url}")

    # Update the User-Agent, API token, and Content-Type.
    headers = {
        "user-agent": scan_agent,
        "Authorization": f"Token {api_token}",
        "Content-Type": "application/json",
    }

    # Convert dictionary to a string, then encode to bytes.
    data = json.dumps(update_info).encode("utf-8")

    # Make the HTTP PATCH request.
    request = urllib.request.Request(method="PATCH", url=url, data=data, headers=headers)
    response = urllib.request.urlopen(request)

    response_code = response.status
    response_data = response.read().decode("utf-8")

    if response_code == 200:
        ROOT_LOGGER.info(f"Successfully updated scan information for scan ID {scan_job_id} with data {update_info}")
        update_scan_information_success = True

    else:
        ROOT_LOGGER.error(
            f"Could not access {master_address}:{master_port} or failed to update scan ID {scan_job_id}. "
            f"HTTP status code: {response_code}"
        )
        ROOT_LOGGER.error(f"Response content: {response_data}")
        update_scan_information_success = False

    return update_scan_information_success


def scan_site(scan_job_dict):
    """Start a scan."""

    try:
        # Unpack the scan_job_dict dictionary.
        scan_job = scan_job_dict["scan_job"]
        config_data = scan_job_dict["config_data"]

        # Assign variables.
        scan_job_id = scan_job["id"]
        scan_status = scan_job["scan_status"]
        site_name = scan_job["site_name"]
        scan_binary = scan_job["scan_binary"]
        scan_command = scan_job["scan_command"]
        result_file_base_name = scan_job["result_file_base_name"]

        http_useragent = config_data["http_useragent"]
        scan_results_dir = config_data["scan_results_dir"]
        target_files_dir = config_data["target_files_dir"]
        target_file = os.path.join(target_files_dir, f"{result_file_base_name}.targets")

        # Setup folder structure.
        pending_files_dir = os.path.join(scan_results_dir, "pending")
        completed_files_dir = os.path.join(scan_results_dir, "complete")
        cancelled_files_dir = os.path.join(scan_results_dir, "cancelled")

        # A request to pause or cancel a scan has been detected.
        if scan_status in ["pause", "cancel"]:

            ROOT_LOGGER.info(f"Received request to {scan_status} scan_job: {scan_job}")

            # Extract the process ID of the scan binary to kill.
            scan_binary_process_id = scan_job["scan_binary_process_id"]

            try:
                # Extract the subprocess.Popen() object based off the scan_binary_process_id key.
                process = SCAN_PROCESS_DICT[scan_binary_process_id]

                # Ensure the scan binary name is one of the supported scan binaries.
                if process.args[0] in config_data["supported_scan_binaries"]:
                    process.kill()
                    stdout, stderr = process.communicate()

                    if not stdout and not stderr:
                        ROOT_LOGGER.info(
                            f"Killed process ID {scan_binary_process_id}.  Command: {' '.join(process.args)}"
                        )

                    else:
                        ROOT_LOGGER.error(
                            f"Issue killing process ID {scan_binary_process_id}.  stderr: {stderr}.  stdout: {stdout}"
                        )

                    # Remove the killed process ID from the scan process dictionary.
                    SCAN_PROCESS_DICT.pop(scan_binary_process_id)

                    if scan_status == "cancel":
                        # Move scan files to the "cancelled" directory for historical purposes.
                        move_wildcard_files(f"{result_file_base_name}*", pending_files_dir, cancelled_files_dir)

                        updated_scan_status = "cancelled"

                    elif scan_status == "pause":
                        updated_scan_status = "paused"

            except KeyError:
                ROOT_LOGGER.error(f"Process ID {scan_binary_process_id} is not running.")

            # Update Master with the updated scan status.
            update_info = {
                "scan_status": updated_scan_status,
            }
            update_scan_information(scan_job, update_info)

            return

        # Write targets to a file.
        # "Passing a huge list of hosts is often awkward on the command line...Each entry must be separated by one or
        # more spaces, tabs, or newlines."
        # https://nmap.org/book/man-target-specification.html
        targets = scan_job["targets"]  # Extract string of targets.

        with open(target_file, "w") as fh:
            fh.write(f"{targets}")

        # Write excluded targets to file if specified.
        excluded_targets = scan_job["excluded_targets"]  # Extract string of targets.
        excluded_target_file = None

        if excluded_targets:
            excluded_target_file = os.path.join(target_files_dir, f"{result_file_base_name}.excluded_targets")
            with open(excluded_target_file, "w") as fh:
                fh.write(f"{excluded_targets}")

        if scan_binary == "masscan":
            # Output format.
            # xml_file = os.path.join(pending_files_dir, f"{result_file_base_name}.xml")
            json_file = os.path.join(pending_files_dir, f"{result_file_base_name}.json")

            # Check if the paused.conf file already exists and resume scan.
            # Only 1 paused.conf file exists, and can be overwritten with a different scan.
            if os.path.isfile("paused.conf"):
                with open("paused.conf", "r") as fh:
                    paused_file = fh.read()

                    # Move back to the beginning of the file.
                    fh.seek(0, 0)

                    paused_file_lines = fh.readlines()

                ROOT_LOGGER.info(f"Previous paused.conf scan file found: {paused_file}")

                # Need to check if output-filename is the same as json_file.
                paused_file_output_filename = None
                for line in paused_file_lines:
                    if line.startswith("output-filename"):
                        paused_file_output_filename = line.split(" = ")[1].strip()

                ROOT_LOGGER.info("Checking if the output-filename is the same.")

                if paused_file_output_filename == json_file:
                    ROOT_LOGGER.info(
                        f"paused.conf file's output-filename '{paused_file_output_filename}' matches this scan request "
                        f"output filename '{json_file}'"
                    )
                    command = "masscan --resume paused.conf"

                else:
                    ROOT_LOGGER.info(
                        f"paused.conf file's output-filename '{paused_file_output_filename}' does not match this scan "
                        f"request output filename '{json_file}'.  Starting a new masscan scan."
                    )

                    # Build the masscan command.
                    command = build_masscan_command(
                        scan_command, target_file, excluded_target_file, json_file, http_useragent
                    )

            # New scan.
            else:
                # Build the masscan command.
                command = build_masscan_command(
                    scan_command, target_file, excluded_target_file, json_file, http_useragent
                )

        elif scan_binary == "nmap":

            # Check if the gnmap file already exists and resume scan.
            gnmap_file = os.path.join(pending_files_dir, f"{result_file_base_name}.gnmap")

            # Ensure the .gnmap file exists and it is greater than 0 bytes before using it.
            if os.path.isfile(gnmap_file) and (os.path.getsize(gnmap_file) > 0):
                ROOT_LOGGER.info(f"Previous scan file found '{gnmap_file}'.  Resuming the scan.")
                command = f"nmap --resume {gnmap_file}"

            # New scan.
            else:
                # Build the nmap command.
                nmap_results = os.path.join(pending_files_dir, result_file_base_name)

                file_options = f"-iL {target_file} -oA {nmap_results} --script-args http.useragent='{http_useragent}'"
                if excluded_target_file:
                    file_options += f" --excludefile {excluded_target_file}"

                command = f"nmap {scan_command} {file_options}"

        else:
            ROOT_LOGGER.error(f"Invalid scan binary specified: {scan_binary}")
            return

        # Spawn a new process for the scan.
        process = subprocess.Popen(command.split())

        # Extract PID.
        scan_binary_process_id = process.pid

        # Track the process ID and subprocess.Popen() object.
        SCAN_PROCESS_DICT[scan_binary_process_id] = process

        # Start the scan.
        ROOT_LOGGER.info(
            f"Starting scan for site '{site_name}', with process ID {scan_binary_process_id}, and command: {command}"
        )

        # Update Master with the process ID.
        update_info = {
            "scan_status": "started",
            "scan_binary_process_id": scan_binary_process_id,
        }
        update_scan_information(scan_job, update_info)

        process.wait()

        # Scan binary process completed successfully.
        # Move files from "pending" directory to "complete" directory.
        if process.returncode == 0:

            move_wildcard_files(f"{result_file_base_name}*", pending_files_dir, completed_files_dir)

            # Update completed_time, scan_status, and result_file_base_name.
            now_datetime = get_current_time()
            update_info = {
                "completed_time": now_datetime,
                "scan_status": "completed",
                "result_file_base_name": result_file_base_name,
            }

            update_scan_information(scan_job, update_info)

            # Remove the completed process ID from the scan process dictionary.
            SCAN_PROCESS_DICT.pop(scan_binary_process_id)

    except Exception as e:
        ROOT_LOGGER.exception(f"Error with scan ID {scan_job_id}.  Exception: {e}")
        update_info = {"scan_status": "error"}
        update_scan_information(scan_job, update_info)


class Worker(threading.Thread):
    """Worker thread"""

    def __init__(self):
        """Initialize Worker thread."""

        threading.Thread.__init__(self)

    def run(self):
        """Start Worker thread."""

        while True:
            # Grab scan_job_dict off the queue.
            scan_job_dict = agent.queue.get()

            try:
                # Kick off scan.
                scan_site(scan_job_dict)

            except Exception as e:
                ROOT_LOGGER.error(f"Failed to start scan.  Exception: {e}")

            agent.queue.task_done()


class Agent:
    """Main Agent class"""

    def __init__(self, config_file):
        """Initialize Agent class"""

        # Load configuration file.
        self.config_data = self.load_config(config_file)

        # Create queue.
        self.queue = queue.Queue()

    def load_config(self, config_file):
        """Load the agent_config.json file and return a JSON object."""

        if os.path.isfile(config_file):
            with open(config_file) as fh:
                json_data = json.loads(fh.read())
                return json_data

        else:
            ROOT_LOGGER.error(f"'{config_file}' does not exist or contains no data.")
            sys.exit(0)

    def go(self):
        """Start the scan agent."""

        # Assign log level.  See README.md for more information.
        ROOT_LOGGER.setLevel((6 - self.config_data["log_verbosity"]) * 10)

        # Kickoff the threadpool.
        for i in range(self.config_data["number_of_threads"]):
            thread = Worker()
            thread.daemon = True
            thread.start()

        ROOT_LOGGER.info(f"Starting scan agent: {self.config_data['scan_agent']}", exc_info=False)

        while True:

            try:

                ROOT_LOGGER.info(f"Current scan processes being tracked in SCAN_PROCESS_DICT: {SCAN_PROCESS_DICT}")

                # Retrieve any new scan jobs from master through API.
                scan_jobs = check_for_scan_jobs()

                if scan_jobs:
                    for scan_job in scan_jobs:

                        ROOT_LOGGER.info(f"scan_job: {scan_job}")

                        # Create new dictionary that will contain scan_job and config_data information.
                        scan_job_dict = {}
                        scan_job_dict["scan_job"] = scan_job
                        scan_job_dict["config_data"] = self.config_data

                        # Place scan_job_dict on queue.
                        self.queue.put(scan_job_dict)

                        # Allow the job to execute and change status before moving to the next one.
                        time.sleep(5)

                    # Don't wait for threads to finish.
                    # self.queue.join()

                else:
                    ROOT_LOGGER.info(
                        f"No scan jobs found...checking back in {self.config_data['callback_interval_in_seconds']} seconds."
                    )
                    time.sleep(self.config_data["callback_interval_in_seconds"])

            except KeyboardInterrupt:
                break

        ROOT_LOGGER.critical("Stopping Scantron agent")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scantron scan agent")
    parser.add_argument(
        "-c",
        dest="config_file",
        action="store",
        required=False,
        default="agent_config.json",
        help="Configuration file.  Defaults to 'agent_config.json'",
    )

    args = parser.parse_args()

    # Log level is controlled in agent_config.json and assigned after reading that file.
    # Setup file logging
    log_file_handler = logging.FileHandler(os.path.join("logs", "agent.log"))
    log_file_handler.setFormatter(LOG_FORMATTER)
    ROOT_LOGGER.addHandler(log_file_handler)

    # Setup console logging
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(LOG_FORMATTER)
    ROOT_LOGGER.addHandler(console_handler)

    agent = Agent(args.config_file)
    agent.go()
