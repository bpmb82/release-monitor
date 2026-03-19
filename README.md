# Release Monitor

A simple Python application that checks specific Git repositories to see if a new version has been released. If a new version is found, it will check and see if a new docker image is available. If so, it will send a tag to a Helm repo which will then update the appVersions of the charts.

This was made for my personal use but feel free to use this in any way you see fit.

# Usage

## Daemon mode

If the ```CHECK_INTERVAL``` environment variable is found, the script will run in daemon mode. See the ```docker-compose.yml``` file on how to run it as a docker image.

## Single shot

If the ```CHECK_INTERVAL``` environment variable wasn't found, the script will run only once.

# Configuration

## Environment variables

```DISCORD_WEBHOOK_URL```
Discord webhook URL for notifications

```GH_TOKEN```
The Github token of the repository we will update with tags

```MY_REPO```
The repository we want to update

```LOGLEVEL```
Optional, default loglevel is ```INFO```

## config.yaml

Example:

```
  - name: Mosquitto
    source: https://api.github.com/repos/eclipse-mosquitto/mosquitto/tags
    docker_repo: "docker.io/eclipse-mosquitto"
    docker_prefix: ""
    docker_suffix: "-alpine"
```

```name```: The name of the repository (this is used to send the tag to the Helm repo)
```source```: The API URL to the Git repository
```docker_repo```: The link to the docker registry and the image we need (no tag)
```docker_prefix```: Sometimes, a 'version-' or other prefix is added to the docker tag which is not included in the Git release
```docker_suffix```: Sometimes, an '-alpine' or other suffix is added to the docker tag which is not included in the Git release

## Releases.json

The releases found are entered into the ```releases.json``` file in the config folder.