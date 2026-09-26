#!/usr/bin/env python3
"""Builds container images via Cloud Build API (using ADC) and updates Terraform."""

import datetime
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import time
import google.auth
from google.auth.transport.requests import AuthorizedSession
from google.cloud import storage

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TF_DIR = os.path.join(ROOT, "deployment", "terraform")
TFVARS = os.path.join(TF_DIR, "terraform.tfvars")


def get_tf_outputs():
    res = subprocess.run(
        ["terraform", f"-chdir={TF_DIR}", "output", "-json"],
        check=True,
        capture_output=True,
        text=True,
    )
    raw = json.loads(res.stdout)
    return {k: v["value"] for k, v in raw.items()}


def get_git_tag():
    git_sha = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True
    ).strip()
    ts = datetime.datetime.now().strftime("%H%M%S")
    return f"{git_sha}-{ts}"


def create_archive():
    ignore_dirs = {
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".terraform",
        "node_modules",
        "dist",
    }
    ignore_exts = {".pyc", ".tfstate", ".backup"}
    ignore_files = {".DS_Store", "terraform.tfvars", ".env", ".coverage"}

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [
                d
                for d in dirnames
                if d not in ignore_dirs and not d.startswith(".venv")
            ]
            for f in filenames:
                if any(f.endswith(ext) for ext in ignore_exts):
                    continue
                if f in ignore_files or f.startswith(".coverage."):
                    continue
                full_path = os.path.join(dirpath, f)
                rel_path = os.path.relpath(full_path, ROOT)
                tar.add(full_path, arcname=rel_path)

    buf.seek(0)
    return buf


def upload_archive(bucket_name, blob_name, buf):
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    blob.upload_from_file(buf, content_type="application/gzip")
    print(f"Uploaded source archive to gs://{bucket_name}/{blob_name}")


def submit_cloud_build(
    project_id, region, build_sa, staging_bucket, blob_name, registry, tag
):
    creds, _ = google.auth.default()
    session = AuthorizedSession(creds)

    build_payload = {
        "source": {
            "storageSource": {
                "bucket": staging_bucket,
                "object": blob_name,
            }
        },
        "steps": [
            {
                "id": "hr-agent",
                "name": "gcr.io/cloud-builders/docker",
                "args": [
                    "build",
                    "-f",
                    "Dockerfile",
                    "--build-arg",
                    f"AGENT_VERSION={tag}",
                    "-t",
                    f"{registry}/hr-agent:{tag}",
                    ".",
                ],
                "waitFor": ["-"],
            },
            {
                "id": "hr-acl",
                "name": "gcr.io/cloud-builders/docker",
                "args": [
                    "build",
                    "-f",
                    "acl_service/Dockerfile",
                    "--build-arg",
                    f"AGENT_VERSION={tag}",
                    "-t",
                    f"{registry}/hr-acl:{tag}",
                    ".",
                ],
                "waitFor": ["-"],
            },
        ],
        "images": [
            f"{registry}/hr-agent:{tag}",
            f"{registry}/hr-acl:{tag}",
        ],
        "serviceAccount": f"projects/{project_id}/serviceAccounts/{build_sa}",
        "options": {
            "logging": "CLOUD_LOGGING_ONLY",
            "machineType": "E2_HIGHCPU_8",
        },
        "timeout": "1200s",
    }

    url = f"https://cloudbuild.googleapis.com/v1/projects/{project_id}/locations/{region}/builds"
    print(f"Submitting Cloud Build to {url}...")
    resp = session.post(url, json=build_payload)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Cloud Build submission failed: {resp.text}")

    op = resp.json()
    build_id = (
        op.get("metadata", {}).get("build", {}).get("id") or op.get("id")
    )
    print(f"Build submitted successfully! Build ID: {build_id}")
    return build_id, session


def wait_for_build(project_id, region, build_id, session):
    url = f"https://cloudbuild.googleapis.com/v1/projects/{project_id}/locations/{region}/builds/{build_id}"
    print("Waiting for Cloud Build to complete...")
    start_time = time.time()
    while True:
        resp = session.get(url)
        if resp.status_code != 200:
            print(f"Warning: polling returned {resp.status_code}: {resp.text}")
        else:
            build = resp.json()
            status = build.get("status")
            elapsed = int(time.time() - start_time)
            print(f"[{elapsed}s] Build status: {status}")
            if status in ("SUCCESS", "FAILURE", "INTERNAL_ERROR", "TIMEOUT", "CANCELLED"):
                if status == "SUCCESS":
                    print("Build succeeded!")
                    return build
                else:
                    failure_info = build.get("failureInfo", {})
                    raise RuntimeError(f"Build failed with status {status}: {failure_info}")
        time.sleep(5)


def update_tfvars(registry, tag):
    with open(TFVARS, "r", encoding="utf-8") as f:
        content = f.read()

    def set_var(name, val, text):
        pattern = rf'^{name}\s*=.*$'
        repl = f'{name} = "{val}"'
        if re.search(pattern, text, flags=re.MULTILINE):
            return re.sub(pattern, repl, text, flags=re.MULTILINE)
        return text + f'\n{repl}\n'

    content = set_var("agent_image", f"{registry}/hr-agent:{tag}", content)
    content = set_var("acl_image", f"{registry}/hr-acl:{tag}", content)
    content = set_var("agent_version", tag, content)

    with open(TFVARS, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"Updated {TFVARS} with tag {tag}")


def apply_terraform():
    print("Applying updated image configuration to Cloud Run via Terraform...")
    subprocess.run(
        ["terraform", f"-chdir={TF_DIR}", "apply", "-input=false", "-auto-approve"],
        check=True,
    )
    print("Terraform apply completed successfully!")


def main():
    outputs = get_tf_outputs()
    project_id = "jm-01-project"
    region = "asia-southeast1"
    staging_bucket = outputs["build_staging_bucket"]
    registry = outputs["artifact_registry"]
    build_sa = outputs["build_service_account"]

    tag = get_git_tag()
    print(f"Target image tag: {tag}")

    buf = create_archive()
    blob_name = f"source/{tag}.tgz"
    upload_archive(staging_bucket, blob_name, buf)

    build_id, session = submit_cloud_build(
        project_id, region, build_sa, staging_bucket, blob_name, registry, tag
    )
    wait_for_build(project_id, region, build_id, session)

    update_tfvars(registry, tag)
    apply_terraform()


if __name__ == "__main__":
    main()
