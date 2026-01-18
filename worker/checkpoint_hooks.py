# worker/checkpoint_hooks.py
def pre_checkpoint():
    print("Pre-checkpoint hook: flushing state")

def post_restore():
    print("Post-restore hook: resuming")
