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

# --- Configuration & Dynamic Logging ---
LOG_LEVEL_STR = os.environ.get("LOGLEVEL", "INFO").upper()
LOG_LEVEL = getattr(logging, LOG_LEVEL_STR, logging.INFO)

logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S%z'
)
logger = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("CONFIG_PATH", "./config")
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

def load_config():
    logger.debug(f"Loading config from {CONFIG_FILE}")
    if not os.path.exists(CONFIG_FILE):
        logger.error("Configuration file not found.")
        return {"repositories": []}
    with open(CONFIG_FILE, "r") as f:
        return yaml.safe_load(f)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            try: return json.load(f)
            except Exception:
                logger.error("Failed to parse state file.")
                return {}
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=4)

def is_stable_version(tag):
    clean_tag = tag.lstrip('v')
    is_stable = bool(re.match(r'^\d+(\.\d+)*$', clean_tag))
    logger.debug(f"Tag check: {tag} | stable: {is_stable}")
    return is_stable

def docker_tag_exists(repo_config, gh_tag):
    full_repo = repo_config.get("docker_repo")
    prefix = repo_config.get("docker_prefix", "")
    suffix = repo_config.get("docker_suffix", "")
    expected_tag = f"{prefix}{gh_tag.lstrip('v')}{suffix}"
    
    logger.debug(f"Checking registry for {full_repo}:{expected_tag}")
    
    image_path = f"docker://{full_repo}:{expected_tag}"
    
    try:
        result = subprocess.run(
            ['skopeo', 'inspect', '--raw', image_path], 
            capture_output=True, 
            text=True, 
            timeout=30
        )
        return (result.returncode == 0), expected_tag
    except Exception:
        logger.error(f"Skopeo execution failed for {full_repo}")
        return False, expected_tag

# --- GitHub & Workflow Logic ---

def is_workflow_running():
    if not GH_TOKEN or not MY_REPO: 
        return False
    url = f"https://api.github.com/repos/{MY_REPO}/actions/runs?status=queued&status=in_progress"
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        return len(response.json().get("workflow_runs", [])) > 0
    except Exception:
        logger.error("Failed to check GitHub Actions status (API error)")
        return False

def trigger_github_tag(repo_name, final_docker_tag):
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
    formatted_tag = f"{repo_name.lower()}_{final_docker_tag}"
    
    logger.info(f"Triggering GitHub tag: {formatted_tag}")

    try:
        requests.delete(f"https://api.github.com/repos/{MY_REPO}/git/refs/tags/{formatted_tag}", headers=headers, timeout=10)
        
        ref_res = requests.get(f"https://api.github.com/repos/{MY_REPO}/git/refs/heads/{MY_BRANCH}", headers=headers, timeout=10)
        ref_res.raise_for_status()
        sha = ref_res.json()["object"]["sha"]

        payload = {"ref": f"refs/tags/{formatted_tag}", "sha": sha}
        res = requests.post(f"https://api.github.com/repos/{MY_REPO}/git/refs", headers=headers, json=payload, timeout=10)
        return res.status_code == 201
    except Exception:
        logger.error(f"GitHub API tagging failed for {repo_name}")
        return False

# --- Background Worker ---

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
            
            time.sleep(30) # Settle
            if is_workflow_running(): continue

            if trigger_github_tag(repo_name, docker_tag):
                state = load_state()
                state[repo_name] = {"last_tag": gh_tag, "retry_count": 0}
                save_state(state)
                
                if DISCORD_WEBHOOK_URL:
                    try:
                        requests.post(DISCORD_WEBHOOK_URL, json={"content": f"🚀 **New Release:** {repo_name}\n**Tag:** `{docker_tag}`"}, timeout=10)
                    except Exception:
                        logger.error("Discord notification failed.")
                
                logger.info(f"Successfully processed {repo_name}. Cooldown 90s.")
                time.sleep(90)
                break
            else:
                break
        
        update_queue.task_done()

def check_repositories():
    logger.debug("Scanning repositories...")
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
                    logger.info(f"Verified image for {name} ({docker_tag}). Queueing...")
                    update_queue.put((name, docker_tag, gh_tag))
                else:
                    current_retries = repo_state.get("retry_count", 0) + 1
                    state[name] = {"last_tag": gh_tag, "retry_count": current_retries if current_retries <= MAX_RETRIES else 999}
                    updated = True
        except Exception:
            logger.error(f"Check failed for {name}")

    if updated:
        save_state(state)

if __name__ == "__main__":
    if not os.path.exists(CONFIG_PATH):
        os.makedirs(CONFIG_PATH)
    
    # Start de worker
    worker_thread = threading.Thread(target=background_worker, daemon=True)
    worker_thread.start()
    
    if RAW_CHECK_INTERVAL:
        # --- DAEMON MODE ---
        interval = int(RAW_CHECK_INTERVAL)
        logger.info(f"Mode: DAEMON | Interval: {interval}s")
        while True:
            check_repositories()
            logger.debug(f"Cycle complete. Sleeping for {interval}s.")
            time.sleep(interval)
    else:
        # --- SINGLE SHOT MODE ---
        logger.info("Mode: SINGLE SHOT")
        check_repositories()
        time.sleep(1)
        logger.info("Waiting for background worker to finish tasks...")
        update_queue.join()
        
        logger.info("All tasks completed. Clean exit.")