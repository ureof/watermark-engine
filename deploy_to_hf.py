"""
1-Click Automated Deployment Script to Hugging Face Spaces
Usage:
    python deploy_to_hf.py --token <YOUR_HF_TOKEN>
"""

import sys
import argparse
from pathlib import Path

try:
    from huggingface_hub import HfApi, create_repo, upload_folder
except ImportError:
    print("Installing huggingface_hub...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "huggingface_hub"])
    from huggingface_hub import HfApi, create_repo, upload_folder

def deploy(token: str, space_name: str = "watermark-engine"):
    api = HfApi(token=token)
    user = api.whoami()["name"]
    repo_id = f"{user}/{space_name}"
    
    print(f"Creating / connecting to Space: {repo_id}...")
    try:
        create_repo(
            repo_id=repo_id,
            repo_type="space",
            space_sdk="docker",
            private=False,
            token=token,
            exist_ok=True
        )
        print(f"Space {repo_id} ready!")
    except Exception as e:
        print(f"Repo notice: {e}")

    current_dir = Path(__file__).parent.resolve()
    print(f"Uploading files from {current_dir} to {repo_id}...")
    upload_folder(
        repo_id=repo_id,
        repo_type="space",
        folder_path=str(current_dir),
        ignore_patterns=["deploy_to_hf.py", ".git*", "__pycache__*"],
        token=token
    )
    
    space_url = f"https://{user}-{space_name.replace('_', '-')}.hf.space"
    print("\n" + "="*60)
    print("SUCCESS! Hugging Face Space deployed successfully!")
    print(f"Space Repo: https://huggingface.co/spaces/{repo_id}")
    print(f"Direct API URL: {space_url}")
    print("="*60 + "\n")
    return space_url

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", type=str, required=True, help="Hugging Face User Access Token (from https://huggingface.co/settings/tokens)")
    parser.add_argument("--name", type=str, default="watermark-engine", help="Name of the space")
    args = parser.parse_args()
    deploy(args.token, args.name)
