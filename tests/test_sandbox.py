import io
import json
import tarfile

import pytest

from forgeflow.contracts import ExecutionSpec, Scope, digest
from forgeflow.sandbox import DockerSandbox, SandboxFailure, parse_artifact_tar
from forgeflow.templates import template_hash


def spec(template="evt-check", attempt=1, scope=None):
    value = {"source": "SYNTHETIC", "evidence": []}
    return ExecutionSpec(
        scope=scope or Scope(tenant_id="t", workspace_id="w", project_id="aurora"),
        run_id="r",
        step_id="s",
        attempt=attempt,
        template_id=template,
        input_manifest=value,
        input_hash=digest(value),
        code_hash=template_hash(template),
        image="sha256:" + "1" * 64,
    )


def archive(name="result.json", value=b'{"ok":true}', kind=tarfile.REGTYPE, linkname=""):
    target = io.BytesIO()
    with tarfile.open(fileobj=target, mode="w") as stream:
        info = tarfile.TarInfo(name)
        info.type, info.linkname, info.size = kind, linkname, len(value)
        stream.addfile(info, io.BytesIO(value) if info.isreg() else None)
    return target.getvalue()


def test_handle_is_deterministic_and_full_scope_attempt_bound():
    provider = DockerSandbox()
    assert provider.handle(spec()) == provider.handle(spec())
    assert provider.handle(spec()) != provider.handle(spec(attempt=2))
    assert provider.handle(spec()) != provider.handle(
        spec(scope=Scope(tenant_id="t", workspace_id="w", project_id="other"))
    )


def test_absence_error_does_not_confuse_missing_daemon_with_removed_container():
    assert not DockerSandbox._confirmed_absent("docker.sock: no such file or directory", "ff-abc")
    assert not DockerSandbox._confirmed_absent("no such object: someone-else", "ff-abc")
    assert not DockerSandbox._confirmed_absent("no such object: ff-abc-other", "ff-abc")
    assert DockerSandbox._confirmed_absent("error: no such object: ff-abc", "ff-abc")


def test_regular_bounded_json_artifact():
    result = parse_artifact_tar(archive())
    assert json.loads(result.content) == {"ok": True}
    assert result.size == 11


@pytest.mark.parametrize(
    "name,kind,link",
    [
        ("../result.json", tarfile.REGTYPE, ""),
        ("/result.json", tarfile.REGTYPE, ""),
        ("nested/result.json", tarfile.REGTYPE, ""),
        ("result.json", tarfile.SYMTYPE, "/etc/passwd"),
        ("result.json", tarfile.LNKTYPE, "/etc/passwd"),
        ("result.json", tarfile.FIFOTYPE, ""),
    ],
)
def test_artifact_paths_links_special_files_rejected(name, kind, link):
    with pytest.raises(SandboxFailure):
        parse_artifact_tar(archive(name, kind=kind, linkname=link))


def test_missing_docker_does_not_host_execute():
    result = DockerSandbox(docker="/forgeflow-no-such-docker").execute(spec())
    assert result.status == "UNAVAILABLE"
    assert result.cleanup == "NOT_STARTED"
    assert not result.artifacts


def test_changed_source_or_manifest_rejected_before_docker(monkeypatch):
    provider = DockerSandbox()
    monkeypatch.setattr(provider, "resolve_image", lambda: pytest.fail("Docker not permitted"))
    result = provider.execute(spec().model_copy(update={"code_hash": "tampered"}))
    assert result.status == "UNAVAILABLE"
    assert "hash mismatch" in result.reason


def test_cleanup_refuses_unknown_or_forged_resources(monkeypatch):
    provider = DockerSandbox()
    monkeypatch.setattr(provider, "_inspect", lambda handle: {"Config": {"Labels": {}}})
    monkeypatch.setattr(provider, "_command", lambda *args, **kw: pytest.fail("No delete permitted"))
    assert provider.cleanup("beauty-service") is False
    assert provider.cleanup(provider.handle(spec())) is False


def test_cleanup_already_absent_is_success(monkeypatch):
    provider = DockerSandbox()

    def absent(handle):
        raise SandboxFailure("error: no such object: " + handle)

    monkeypatch.setattr(provider, "_inspect", absent)
    assert provider.cleanup(provider.handle(spec())) is True


@pytest.mark.parametrize(
    "stage,expected_cleanup",
    [
        ("ownership", "FAILED"),
        ("inputs", "REMOVED"),
        ("source", "REMOVED"),
        ("output", "REMOVED"),
    ],
)
def test_partial_scratch_failures_are_cleaned_or_explicitly_require_recovery(
    tmp_path, monkeypatch, stage, expected_cleanup
):
    import os
    from pathlib import Path

    import forgeflow.sandbox as module

    provider = DockerSandbox(scratch_root=str(tmp_path / "private"))
    request = spec()
    monkeypatch.setattr(provider, "resolve_image", lambda: request.image)

    def absent(handle):
        raise SandboxFailure("no such object: " + handle)

    monkeypatch.setattr(provider, "_inspect", absent)
    if stage == "ownership":
        original = Path.write_text

        def write(path, *args, **kwargs):
            if path.name == "ownership.json":
                raise OSError("injected ownership write failure")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", write)
    elif stage == "inputs":
        original = Path.mkdir

        def mkdir(path, *args, **kwargs):
            if path.name == "inputs":
                raise OSError("injected input mkdir failure")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", mkdir)
    elif stage == "source":

        def broken(template):
            raise OSError("injected template load failure")

        monkeypatch.setattr(module, "template_source", broken)
    else:
        original = os.open

        def opened(path, *args, **kwargs):
            if Path(path).name == "result.json":
                raise OSError("injected output open failure")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", opened)
    result = provider.execute(request)
    assert result.status == "UNAVAILABLE"
    assert result.cleanup == expected_cleanup
    assert (provider.scratch_root / provider.handle(request)).exists() == (expected_cleanup == "FAILED")


def test_recovered_handle_cannot_late_create_same_attempt(tmp_path, monkeypatch):
    provider = DockerSandbox(scratch_root=str(tmp_path / "private"))
    request = spec()
    monkeypatch.setattr(provider, "resolve_image", lambda: request.image)

    def absent(handle):
        raise SandboxFailure("no such object: " + handle)

    monkeypatch.setattr(provider, "_inspect", absent)
    assert provider.cleanup(provider.handle(request))
    monkeypatch.setattr(
        provider, "_command", lambda *a, **k: pytest.fail("Revoked attempt cannot issue Docker commands")
    )
    result = provider.execute(request)
    assert result.status == "CANCELLED"
    assert result.cleanup == "NOT_STARTED"
