import os
import time
import yaml
import json
import requests
import logging
import re
import subprocess
import threading
import queue
from packaging import version

LOG_LEVEL_STR = os.environ.get("LOGLEVEL", "INFO").upper()
LOG_LEVEL = getattr(logging, LOG_LEVEL_STR, logging.INFO)

logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S%z'
)
logger = logging.getLogger(__name__)

CONFIG_PATH = "./config"
CONFIG_FILE = os.path.join(CONFIG_PATH, "config.yaml")
STATE_FILE = os.path.join(CONFIG_PATH, "releases.json")

# Environment Variables
RAW_CHECK_INTERVAL = os.environ.get("CHECK_INTERVAL", "").strip()
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", 2))
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip('"').strip("'")
GH_TOKEN = os.environ.get("GH_TOKEN", "").strip('"').strip("'")
MY_REPO = os.environ.get("MY_REPO", "").strip('"').strip("'")
MY_BRANCH = os.environ.get("MY_BRANCH", "master").strip('"').strip("'")

update_queue = queue.Queue()

def load_config():
    logger.debug(f"Reading configuration from {CONFIG_FILE}")
    if not os.path.exists(CONFIG_FILE):
        logger.error(f"Configuration file not found at {CONFIG_FILE}")
        return {"repositories": []}
    with open(CONFIG_FILE, "r") as f:
        return yaml.safe_load(f)

def load_state():
    logger.debug(f"Reading state from {STATE_FILE}")
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            try: return json.load(f)
            except Exception as e:
                logger.error(f"Failed to parse state file: {e}")
                return {}
    return {}

def save_state(state):
    logger.debug(f"Saving updated state to {STATE_FILE}")
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=4)

def is_stable_version(tag):
    clean_tag = tag.lstrip('v')

    is_stable = bool(re.match(r'^\d+(\.\d+)*$', clean_tag))
    
    logger.debug(f"Validating tag: {tag} (stable={is_stable})")
    return is_stable

def docker_tag_exists(repo_config, gh_tag):
    full_repo = repo_config.get("docker_repo")
    prefix = repo_config.get("docker_prefix", "")
    suffix = repo_config.get("docker_suffix", "")
    expected_tag = f"{prefix}{gh_tag.lstrip('v')}{suffix}"
    
    image_path = f"docker://{full_repo}:{expected_tag}"
    logger.debug(f"Probing registry with Skopeo: {image_path}")
    
    try:
        result = subprocess.run(['skopeo', 'inspect', '--raw', image_path], capture_output=True, text=True, timeout=30)
        exists = (result.returncode == 0)
        return exists, expected_tag
    except Exception as e:
        logger.error(f"Skopeo process error: {e}")
        return False, expected_tag

def is_workflow_running():
    if not GH_TOKEN or not MY_REPO: 
        return False
    url = f"https://api.github.com/repos/{MY_REPO}/actions/runs?status=queued&status=in_progress"
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        data = response.json()
        active_runs = len(data.get("workflow_runs", []))
        return active_runs > 0
    except Exception as e:
        logger.error(f"Error calling GitHub Actions API: {e}")
        return False

def trigger_github_tag(repo_name, final_docker_tag):
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
    formatted_tag = f"{repo_name.lower()}_{final_docker_tag}"
    try:
        del_url = f"https://api.github.com/repos/{MY_REPO}/git/refs/tags/{formatted_tag}"
        requests.delete(del_url, headers=headers)
        ref_url = f"https://api.github.com/repos/{MY_REPO}/git/refs/heads/{MY_BRANCH}"
        sha_res = requests.get(ref_url, headers=headers)
        sha_res.raise_for_status()
        sha = sha_res.json()["object"]["sha"]
        payload = {"ref": f"refs/tags/{formatted_tag}", "sha": sha}
        res = requests.post(f"https://api.github.com/repos/{MY_REPO}/git/refs", headers=headers, json=payload)
        return res.status_code == 201
    except Exception as e:
        logger.error(f"Exception in trigger_github_tag: {e}")
        return False

def background_worker():
    logger.info(f"Background worker active.")
    while True:
        task = update_queue.get()
        repo_name, docker_tag, gh_tag = task
        
        while True:
            if is_workflow_running():
                logger.info(f"GitHub Action is busy. Waiting 2 minutes...")
                time.sleep(120)
                continue
            
            time.sleep(30) # Settle time
            
            if is_workflow_running():
                continue

            if trigger_github_tag(repo_name, docker_tag):
                state = load_state()
                state[repo_name] = {"last_tag": gh_tag, "retry_count": 0}
                save_state(state)
                if DISCORD_WEBHOOK_URL:
                    requests.post(DISCORD_WEBHOOK_URL, json={"content": f"🚀 **New Release Triggered!**\n**Service:** {repo_name}\n**Version:** `{docker_tag}`"})
                
                logger.info(f"Update pushed for {repo_name}. Entering 90s cooldown...")
                time.sleep(90)
                break
            else:
                logger.error(f"Task for {repo_name} failed.")
                break
        
        update_queue.task_done()

def check_repositories():
    logger.debug("Initiating repository scan...")
    config = load_config()
    state = load_state()
    updated = False
    current_queue_names = [item[0] for item in list(update_queue.queue)]

    for repo in config.get("repositories", []):
        name = repo.get("name")
        if not name or name in current_queue_names:
            continue

        try:
            res = requests.get(repo.get("source"), headers={"User-Agent": "Release-Monitor-Bot"}, timeout=10)
            if res.status_code != 200: continue
            
            data = res.json()
            gh_tag = data[0].get("name") if isinstance(data, list) else data.get("tag_name")
            if not gh_tag or not is_stable_version(gh_tag): continue

            repo_state = state.get(name, {"last_tag": None, "retry_count": 0})

            if repo_state["last_tag"] != gh_tag or (0 < repo_state["retry_count"] <= MAX_RETRIES):
                exists, docker_tag = docker_tag_exists(repo, gh_tag)
                if exists:
                    logger.info(f"Verified! Docker image for {name} exists. Adding to queue.")
                    update_queue.put((name, docker_tag, gh_tag))
                else:
                    current_retries = repo_state.get("retry_count", 0) + 1
                    state[name] = {"last_tag": gh_tag, "retry_count": current_retries if current_retries <= MAX_RETRIES else 999}
                    updated = True
        except Exception as e:
            logger.error(f"Error checking {name}: {e}")

    if updated:
        save_state(state)

if __name__ == "__main__":
    if not os.path.exists(CONFIG_PATH):
        os.makedirs(CONFIG_PATH)
    
    worker_thread = threading.Thread(target=background_worker, daemon=True)
    worker_thread.start()
    
    if RAW_CHECK_INTERVAL:
        # --- DAEMON MODE ---
        interval = int(RAW_CHECK_INTERVAL)
        logger.info(f"Release Monitor: DAEMON mode (Interval: {interval}s)")
        while True:
            check_repositories()
            logger.debug(f"Cycle complete. Sleeping for {interval}s.")
            time.sleep(interval)
    else:
        # --- SINGLE SHOT MODE ---
        logger.info("Release Monitor: SINGLE SHOT mode")
        check_repositories()
        
        if not update_queue.empty():
            logger.info(f"Waiting for {update_queue.qsize()} background task(s) to finish...")
            update_queue.join()
            logger.info("All background tasks completed.")
        else:
            logger.info("No updates to process.")
        
        logger.info("Clean exit.")