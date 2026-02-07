import boto3
import paramiko
import time
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# AWS Configuration from environment variables
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
EC2_INSTANCE_ID = os.getenv("EC2_INSTANCE_ID")
SSH_KEY_PATH = os.getenv("SSH_KEY_PATH")
SSH_USERNAME = os.getenv("SSH_USERNAME", "ec2-user")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")

# Validate required environment variables
required_vars = {
    "EC2_INSTANCE_ID": EC2_INSTANCE_ID,
    "SSH_KEY_PATH": SSH_KEY_PATH,
    "AWS_ACCESS_KEY_ID": AWS_ACCESS_KEY_ID,
    "AWS_SECRET_ACCESS_KEY": AWS_SECRET_ACCESS_KEY
}

missing_vars = [var for var, value in required_vars.items() if not value]
if missing_vars:
    raise EnvironmentError(
        f"Missing required environment variables: {', '.join(missing_vars)}\n"
        f"Please set them in your .env file or environment."
    )

# EC2 client
ec2 = boto3.client('ec2', region_name=AWS_REGION, aws_access_key_id=AWS_ACCESS_KEY_ID, aws_secret_access_key=AWS_SECRET_ACCESS_KEY)

def get_instance_public_ip(instance_id):
    response = ec2.describe_instances(InstanceIds=[instance_id])
    return response['Reservations'][0]['Instances'][0]['PublicIpAddress']

def ssh_connect(public_ip):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(public_ip, username=SSH_USERNAME, key_filename=SSH_KEY_PATH)
    return ssh

def ensure_instance_running(instance_id):
    response = ec2.describe_instances(InstanceIds=[instance_id])
    state = response['Reservations'][0]['Instances'][0]['State']['Name']
    
    if state == 'stopped':
        print(f"Instance {instance_id} is stopped. Starting it...")
        ec2.start_instances(InstanceIds=[instance_id])
        waiter = ec2.get_waiter('instance_running')
        print("Waiting for instance to start...")
        waiter.wait(InstanceIds=[instance_id])
        print("Instance started successfully.")
    elif state != 'running':
        raise Exception(f"Instance is in {state} state. Unable to proceed.")
    else:
        print("Instance is already running.")

def run_command(channel, command, timeout = 1):
    channel.send(command + "\n")
    time.sleep(timeout)  # Give some time for the command to execute
    output = channel.recv(1024).decode()
    print(output)
    return output

def main():
    # Ensure the instance is running
    ensure_instance_running(EC2_INSTANCE_ID)
    # Get the public IP of the EC2 instance
    public_ip = get_instance_public_ip(EC2_INSTANCE_ID)
    print(f"Connecting to EC2 instance at {public_ip}")

    # Connect to the instance
    ssh = ssh_connect(public_ip)

    try:
        # Stop the service
         with ssh.invoke_shell() as channel:
            print("Stopping stock_analysis.service...")
            run_command(channel, "sudo systemctl stop stock_analysis.service")

            print("Changing to StockAnalysis directory...")
            run_command(channel, "cd /home/ec2-user/StockAnalysis")

            print("Current directory:")
            run_command(channel, "pwd")

            print("Listing directory contents:")
            run_command(channel, "ls -l")

            print("Pulling latest changes from Git...")
            run_command(channel, "git pull", timeout=5)

            # Start the service
            print("Starting stock_analysis.service...")
            run_command(channel, "sudo systemctl start stock_analysis.service")

            # Check the status of the service
            print("Checking service status...")
            run_command(channel, "sudo systemctl status stock_analysis.service" ,timeout=5)

    finally:
        # Close the SSH connection
        ssh.close()

if __name__ == "__main__":
    main()