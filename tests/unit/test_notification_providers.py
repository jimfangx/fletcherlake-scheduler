"""Official provider envelopes, safe errors and redirects use intercepted HTTPS requests."""

import json
from email import policy
from email.parser import BytesParser
from uuid import uuid4

import httpx
import pytest
from fl_common.models.base import utcnow
from fl_scheduler.notifications.config import load_settings
from fl_scheduler.notifications.content import Notice
from fl_scheduler.notifications.providers import providers

from tests.notification_helpers import CHAT, MAILGUN_KEY, SLACK, settings, success


@pytest.mark.parametrize("channel", ["mailgun", "slack", "google_chat"])
async def test_provider_envelope_and_positive_ack(channel):
    calls = []

    def handle(request):
        calls.append(request)
        return success(request)

    notification_id = uuid4()
    notice = Notice(
        event_id=1,
        event_type="JOB_SUCCEEDED",
        timestamp=utcnow(),
        job_id=uuid4(),
        recipient="alice@example.edu",
    )
    config = settings((channel,))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = next(iter(providers(config, client).values()))
        result = await provider.send(notification_id, notice, "https://scheduler.test")
    assert result.state == "SENT" and len(calls) == 1
    request = calls[0]
    assert request.method == "POST" and request.url.scheme == "https"
    assert MAILGUN_KEY not in request.content.decode()
    if channel == "mailgun":
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: "
            + request.headers["Content-Type"].encode()
            + b"\r\n\r\n"
            + request.content
        )
        fields = {
            part.get_param("name", header="content-disposition"): part.get_content()
            for part in message.iter_parts()
        }
        assert fields["to"] == "alice@example.edu"
        assert fields["h:Message-Id"] == f"<fl-{notification_id}@mail.example.edu>"
        assert request.headers["Authorization"].startswith("Basic ")
    else:
        body = json.loads(request.content)
        assert "alice@example.edu" not in body["text"]
        if channel == "slack":
            assert body["blocks"][0]["text"]["type"] == "plain_text"
            assert request.url == SLACK
        else:
            assert request.url.params["requestId"] == str(notification_id)
            assert (
                request.url.params["key"] == "chat-key"
                and request.url.params["token"] == "chat-secret"
            )
        assert "Authorization" not in request.headers


@pytest.mark.parametrize("channel", ["mailgun", "slack", "google_chat"])
@pytest.mark.parametrize(
    "status,state",
    [(429, "RETRY"), (503, "RETRY"), (401, "FAILED"), (302, "FAILED"), (200, "RETRY")],
)
async def test_provider_errors_are_safe_and_redirects_never_forward_credentials(
    channel, status, state
):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            status,
            text="secret-bearing invalid response",
            headers={"Location": "https://other.test/steal", "Retry-After": "7"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), follow_redirects=True
    ) as client:
        provider = next(iter(providers(settings((channel,)), client).values()))
        result = await provider.send(
            uuid4(),
            Notice(
                event_id=1,
                event_type="JOB_FAILED",
                timestamp=utcnow(),
                recipient="alice@example.edu",
            ),
            "https://scheduler.test",
        )
    assert result.state == state and len(calls) == 1
    assert "secret" not in str(result)
    assert result.retry_after == (7 if status in {429, 503} else 0)


def test_notification_settings_reject_public_files_symlinks_and_secret_bearing_validation_errors(
    tmp_path,
):
    path = tmp_path / "notifications.json"
    configuration = settings().model_dump(mode="json")
    configuration["mailgun"]["api_key"] = MAILGUN_KEY
    configuration["webhooks"][0]["url"] = SLACK
    configuration["webhooks"][1]["url"] = CHAT
    path.write_text(json.dumps(configuration))
    path.chmod(0o600)
    assert load_settings(path).mailgun.api_key.get_secret_value() == MAILGUN_KEY
    path.chmod(0o644)
    with pytest.raises(RuntimeError, match="protected"):
        load_settings(path)
    path.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        load_settings(link)
    data = json.loads(path.read_text())
    data["webhooks"][0]["url"] = "https://other.test/SECRET-CREDENTIAL"
    path.write_text(json.dumps(data))
    with pytest.raises(RuntimeError) as error:
        load_settings(path)
    assert "SECRET-CREDENTIAL" not in str(error.value)
    assert "secret" not in repr(settings())


def test_provider_identity_survives_secret_rotation_without_rerouting():
    from pydantic import SecretStr

    first = settings()
    rotated = settings()
    rotated.mailgun.api_key = SecretStr("rotated")
    rotated.webhooks[0].url = SecretStr(SLACK.replace("slack-secret", "rotated"))
    rotated.webhooks[1].url = SecretStr(CHAT.replace("chat-secret", "rotated"))
    client = httpx.AsyncClient()
    assert providers(first, client).keys() == providers(rotated, client).keys()
