import os


def validate(path="/opt/job_workspace/checkpoint"):
    required = ["core-1.img", "inventory.img"]
    for f in required:
        if not os.path.exists(os.path.join(path, f)):
            raise RuntimeError("Invalid checkpoint")
    return True
