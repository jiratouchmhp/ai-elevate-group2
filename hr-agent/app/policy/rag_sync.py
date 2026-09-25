"""Sync the governed handbook corpus into Vertex AI RAG Engine (SDD D3, §3.7, FR-5.1).

Our deterministic ingestion (`app.policy.ingest`) stays the source of truth for
chunking and metadata (C-1 re-titling, C-2 authority, C-3 quarantine, C-4 hash
anchors, C-6 jurisdiction). RAG Engine only embeds and searches: every chunk is
uploaded as its own object `gs://<bucket>/<corpus_version>/<anchor>.md` and imported
with a chunk size larger than any chunk, so RAG Engine never re-splits it and a
retrieved `source_uri` maps straight back to the local `Chunk` (and its citation).

Idempotent: if the corpus already holds exactly the current anchors, nothing changes.

    uv run python -m app.policy.rag_sync --project jm-01-project \
        --bucket <project>-hr-policy-corpus [--write-tfvars deployment/terraform/terraform.tfvars]
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from app import config
from app.policy.ingest import Chunk, load_corpus
from app.policy.retriever import chunk_object_name

CORPUS_DISPLAY_NAME = "altostrat-hr-handbook"
# text-embedding-005 accepts up to 2048 tokens; the largest chunk is ~1.5k tokens.
RAG_CHUNK_SIZE = 2048


def render(chunk: Chunk) -> str:
    """Object body: parent-section context prepended so the embedding keeps its meaning (§3.7)."""
    return (
        f"# {chunk.citation_label}\n"
        f"Section: {chunk.section_number} {chunk.section_title}\n"
        f"Jurisdiction: {chunk.jurisdiction} | Authority: {chunk.authority}\n\n"
        f"{chunk.text}\n"
    )


def upload(bucket_name: str, prefix: str, chunks: list[Chunk]) -> str:  # pragma: no cover - network
    from google.cloud import storage

    bucket = storage.Client().bucket(bucket_name)
    for c in chunks:
        bucket.blob(f"{prefix}/{chunk_object_name(c)}").upload_from_string(
            render(c), content_type="text/markdown")
    return f"gs://{bucket_name}/{prefix}/"


def grant_rag_reader(project: str, bucket_name: str) -> None:  # pragma: no cover - network
    """Let RAG Engine's service agent (created with the first corpus) read the corpus bucket."""
    from google.cloud import resourcemanager_v3, storage

    number = resourcemanager_v3.ProjectsClient().get_project(name=f"projects/{project}").name.split("/")[-1]
    member = f"serviceAccount:service-{number}@gcp-sa-vertex-rag.iam.gserviceaccount.com"
    bucket = storage.Client(project=project).bucket(bucket_name)
    policy = bucket.get_iam_policy(requested_policy_version=3)
    role = "roles/storage.objectViewer"
    if any(b["role"] == role and member in b["members"] for b in policy.bindings):
        return
    policy.bindings.append({"role": role, "members": {member}})
    bucket.set_iam_policy(policy)
    print(f"granted {role} on gs://{bucket_name} to {member}")


def sync(project: str, location: str, bucket: str) -> str:  # pragma: no cover - network
    import vertexai
    from vertexai import rag

    vertexai.init(project=project, location=location)
    report = load_corpus()
    wanted = {chunk_object_name(c) for c in report.chunks}

    corpus = next((c for c in rag.list_corpora() if c.display_name == CORPUS_DISPLAY_NAME), None)
    if corpus is None:
        corpus = rag.create_corpus(
            display_name=CORPUS_DISPLAY_NAME,
            description="Altostrat Singapore Employee Policy Handbook — one file per governed chunk",
            backend_config=rag.RagVectorDbConfig(
                # vector_db omitted: RagManagedDb is the service default, and passing
                # rag.RagManagedDb() hits an SDK proto-plus bug (no CopyFrom) in aiplatform 1.165.
                rag_embedding_model_config=rag.RagEmbeddingModelConfig(
                    vertex_prediction_endpoint=rag.VertexPredictionEndpoint(
                        publisher_model=config.RAG_EMBEDDING_MODEL)),
            ),
        )
        print(f"created corpus {corpus.name}")

    existing = list(rag.list_files(corpus_name=corpus.name))
    have = {f.display_name for f in existing}
    if have == wanted:
        print(f"corpus up to date (corpus_version={report.corpus_version}, files={len(have)})")
        return corpus.name

    uri = upload(bucket, report.corpus_version, report.chunks)
    grant_rag_reader(project, bucket)
    for f in existing:  # stale chunks from a previous corpus_version
        rag.delete_file(name=f.name)
    from google.api_core import exceptions as gexc

    resp = None
    for attempt in range(6):  # IAM on the just-created RAG service agent can take minutes to propagate
        try:
            resp = rag.import_files(
                corpus.name, paths=[uri],
                transformation_config=rag.TransformationConfig(
                    chunking_config=rag.ChunkingConfig(chunk_size=RAG_CHUNK_SIZE, chunk_overlap=0)),
            )
            break
        except (gexc.InternalServerError, gexc.ServiceUnavailable, gexc.PermissionDenied,
                gexc.FailedPrecondition) as exc:
            if attempt == 5:
                raise
            wait = 30 * (attempt + 1)
            print(f"import attempt {attempt + 1} failed ({exc.__class__.__name__}); retrying in {wait}s")
            time.sleep(wait)
    print(f"imported {resp.imported_rag_files_count} files "
          f"(skipped {resp.skipped_rag_files_count}, failed {resp.failed_rag_files_count}) from {uri}")
    if resp.failed_rag_files_count:
        sys.exit("RAG import had failures — see Cloud Logging")
    return corpus.name


def write_tfvar(path: Path, key: str, value: str) -> None:
    text = path.read_text() if path.exists() else ""
    line = f'{key} = "{value}"'
    pattern = re.compile(rf"^{re.escape(key)}\s*=.*$", re.MULTILINE)
    text = pattern.sub(line, text) if pattern.search(text) else text.rstrip("\n") + f"\n{line}\n"
    path.write_text(text.lstrip("\n"))


def main() -> None:  # pragma: no cover - CLI
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project", required=True)
    p.add_argument("--location", default=config.RAG_LOCATION)
    p.add_argument("--bucket", default=config.POLICY_CORPUS_BUCKET, required=not config.POLICY_CORPUS_BUCKET)
    p.add_argument("--write-tfvars", type=Path, help="update rag_corpus in this tfvars file")
    a = p.parse_args()
    name = sync(a.project, a.location, a.bucket)
    print(f"RAG_CORPUS={name}")
    if a.write_tfvars:
        write_tfvar(a.write_tfvars, "rag_corpus", name)
        print(f"wrote rag_corpus to {a.write_tfvars}")


if __name__ == "__main__":  # pragma: no cover
    main()
