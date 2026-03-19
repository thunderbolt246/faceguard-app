import psutil
import time
import subprocess

SERVICE_NAME = "VakratmaBiometric"

def is_service_running():

    try:

        result = subprocess.run(
            ["sc","query",SERVICE_NAME],
            capture_output=True,
            text=True
        )

        if "RUNNING" in result.stdout:

            return True

    except:

        pass

    return False


while True:

    try:

        if not is_service_running():

            print("Biometric service stopped. Restarting...")

            subprocess.Popen(
                ["net","start",SERVICE_NAME],
                shell=True
            )

        time.sleep(5)

    except:

        time.sleep(5)