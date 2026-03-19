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

# --- Configuration & Logging ---
LOG_LEVEL_STR = os.environ.get("LOGLEVEL", "INFO").upper()
LOG_LEVEL = getattr(logging, LOG_LEVEL_STR, logging.INFO)

logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S%z'
)
logger = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config")
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

# --- Helper Functions ---

def clean_version_tag(tag):
    """Removes 'v' or 'Sabnzbd ' (case-insensitive) from the start of the tag."""
    if not tag: return ""
    return re.sub(r'^(v|Sabnzbd\s+)', '', tag, flags=re.IGNORECASE).strip()

def is_stable_version(tag):
    """Checks if the tag consists only of numbers and dots after cleaning."""
    clean_tag = clean_version_tag(tag)
    is_stable = bool(re.match(r'^\d+(\.\d+)*$', clean_tag))
    logger.debug(f"Tag validation: {tag} -> stable: {is_stable}")
    return is_stable

def load_config():
    if not os.path.exists(CONFIG_FILE):
        logger.error(f"Configuration not found: {CONFIG_FILE}")
        return {"repositories": []}
    with open(CONFIG_FILE, "r") as f:
        return yaml.safe_load(f)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            try: return json.load(f)
            except: 
                logger.error("Failed to parse state file, starting fresh.")
                return {}
    return {}

def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=4)
        logger.debug(f"State saved to {STATE_FILE}")
    except Exception:
        logger.error("Failed to save state file.")

def docker_tag_exists(repo_config, gh_tag):
    full_repo = repo_config.get("docker_repo")
    prefix = repo_config.get("docker_prefix", "")
    suffix = repo_config.get("docker_suffix", "")
    
    clean_tag = clean_version_tag(gh_tag)
    expected_tag = f"{prefix}{clean_tag}{suffix}"
    
    image_path = f"docker://{full_repo}:{expected_tag}"
    logger.debug(f"Checking registry: {full_repo}:{expected_tag}")
    
    try:
        result = subprocess.run(['skopeo', 'inspect', '--raw', image_path], capture_output=True, text=True, timeout=30)
        return (result.returncode == 0), expected_tag
    except Exception:
        logger.error(f"Skopeo error while checking {full_repo}")
        return False, expected_tag

# --- GitHub & Logic ---

def is_workflow_running():
    if not GH_TOKEN or not MY_REPO: return False
    url = f"https://api.github.com/repos/{MY_REPO}/actions/runs?status=queued&status=in_progress"
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        return len(response.json().get("workflow_runs", [])) > 0
    except:
        return False

def trigger_github_tag(repo_name, final_docker_tag):
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
    formatted_tag = f"{repo_name.lower()}_{final_docker_tag}"
    try:
        requests.delete(f"https://api.github.com/repos/{MY_REPO}/git/refs/tags/{formatted_tag}", headers=headers, timeout=10)
        
        ref_res = requests.get(f"https://api.github.com/repos/{MY_REPO}/git/refs/heads/{MY_BRANCH}", headers=headers, timeout=10)
        ref_res.raise_for_status()
        sha = ref_res.json()["object"]["sha"]

        payload = {"ref": f"refs/tags/{formatted_tag}", "sha": sha}
        res = requests.post(f"https://api.github.com/repos/{MY_REPO}/git/refs", headers=headers, json=payload, timeout=10)
        return res.status_code == 201
    except:
        logger.error(f"Error while tagging {repo_name} on GitHub")
        return False

def background_worker():
    logger.info("Background worker initialized.")
    while True:
        task = update_queue.get()
        repo_name, docker_tag, gh_tag = task
        
        while True:
            if is_workflow_running():
                logger.info("GitHub Action busy. Waiting 2 minutes...")
                time.sleep(120)
                continue
            
            time.sleep(30) # Settle time
            if is_workflow_running(): continue

            if trigger_github_tag(repo_name, docker_tag):
                state = load_state()
                state[repo_name] = {"last_tag": gh_tag, "retry_count": 0}
                save_state(state)
                
                if DISCORD_WEBHOOK_URL:
                    try: 
                        msg = {"content": f"🚀 **Update:** {repo_name} triggered to version `{docker_tag}`"}
                        requests.post(DISCORD_WEBHOOK_URL, json=msg, timeout=10)
                    except: 
                        pass
                
                logger.info(f"Successfully processed {repo_name}. Entering 90s cooldown.")
                time.sleep(90)
                break
            else:
                logger.error(f"Failed to trigger update for {repo_name}.")
                break
        update_queue.task_done()

def check_repositories():
    logger.debug("Starting repository scan...")
    config = load_config()
    state = load_state()
    updated = False
    current_queue_names = [item[0] for item in list(update_queue.queue)]

    for repo in config.get("repositories", []):
        name = repo.get("name")
        if not name or name in current_queue_names: continue

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
                    logger.info(f"New version verified for {name}: {docker_tag}")
                    update_queue.put((name, docker_tag, gh_tag))
                else:
                    current_retries = repo_state.get("retry_count", 0) + 1
                    logger.warning(f"Docker image {name}:{docker_tag} not found yet (attempt {current_retries})")
                    state[name] = {"last_tag": gh_tag, "retry_count": current_retries if current_retries <= MAX_RETRIES else 999}
                    updated = True
        except Exception:
            logger.error(f"Check failed for repository: {name}")

    if updated:
        save_state(state)

if __name__ == "__main__":
    if not os.path.exists(CONFIG_PATH):
        os.makedirs(CONFIG_PATH)
    
    threading.Thread(target=background_worker, daemon=True).start()
    
    if RAW_CHECK_INTERVAL:
        interval = int(RAW_CHECK_INTERVAL)
        logger.info(f"Mode: DAEMON | Interval: {interval}s")
        while True:
            check_repositories()
            time.sleep(interval)
    else:
        logger.info("Mode: SINGLE SHOT")
        check_repositories()

        time.sleep(2)
        
        if not update_queue.empty():
            logger.info(f"Waiting for {update_queue.qsize()} background task(s) to finish...")
            update_queue.join()
        
        logger.info("All tasks completed. Clean exit.")