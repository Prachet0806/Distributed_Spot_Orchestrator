import os
from common_constants import CHECKPOINT_DIR, REQUIRED_CHECKPOINT_FILES


def validate(path=CHECKPOINT_DIR):
    """
    Validate that a checkpoint directory contains required CRIU image files.
    
    Args:
        path: Checkpoint directory path (default from common_constants)
        
    Returns:
        True if valid
        
    Raises:
        RuntimeError: If checkpoint is invalid or incomplete
    """
    if not os.path.exists(path):
        raise RuntimeError(f"Checkpoint directory not found: {path}")
    
    for f in REQUIRED_CHECKPOINT_FILES:
        file_path = os.path.join(path, f)
        if not os.path.exists(file_path):
            raise RuntimeError(f"Missing required checkpoint file: {f}")
    
    return True
