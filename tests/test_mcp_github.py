import io
import json
import unittest
from urllib.error import HTTPError, URLError

from envy.mcp.github import GitHubRepository, create_pull_request, parse_github_remote


class Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class RawResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return self.payload


class GitHubTests(unittest.TestCase):
    def test_parse_https_and_ssh_remotes(self) -> None:
        self.assertEqual(
            parse_github_remote("https://github.com/acme/project.git"),
            GitHubRepository(owner="acme", name="project"),
        )
        self.assertEqual(
            parse_github_remote("git@github.com:acme/project.git"),
            GitHubRepository(owner="acme", name="project"),
        )

    def test_parse_rejects_non_github_and_malformed_remotes(self) -> None:
        with self.assertRaisesRegex(ValueError, "github.com"):
            parse_github_remote("https://gitlab.com/acme/project.git")
        with self.assertRaisesRegex(ValueError, "could not parse"):
            parse_github_remote("https://github.com/acme")

    def test_create_pull_request_reports_configuration_and_network_errors(self) -> None:
        repository = GitHubRepository(owner="acme", name="project")
        with self.assertRaisesRegex(ValueError, "token is empty"):
            create_pull_request(
                repository,
                token="",
                head="feature",
                base="main",
                title="Title",
                body="",
                draft=False,
            )

        def rejected(_request, *, timeout):
            raise HTTPError(
                "https://api.github.com",
                403,
                "forbidden",
                None,
                io.BytesIO(b'{"message":"forbidden"}'),
            )

        with self.assertRaisesRegex(RuntimeError, "forbidden"):
            create_pull_request(
                repository,
                token="token",
                head="feature",
                base="main",
                title="Title",
                body="",
                draft=False,
                opener=rejected,
            )

        def unreachable(_request, *, timeout):
            raise URLError("offline")

        with self.assertRaisesRegex(RuntimeError, "could not reach GitHub"):
            create_pull_request(
                repository,
                token="token",
                head="feature",
                base="main",
                title="Title",
                body="",
                draft=False,
                opener=unreachable,
            )

    def test_create_pull_request_rejects_invalid_responses(self) -> None:
        repository = GitHubRepository(owner="acme", name="project")

        for payload in (b"not-json", b"\xff"):
            with (
                self.subTest(payload=payload),
                self.assertRaisesRegex(RuntimeError, "invalid pull request response"),
            ):
                create_pull_request(
                    repository,
                    token="token",
                    head="feature",
                    base="main",
                    title="Title",
                    body="",
                    draft=False,
                    opener=lambda _request, *, timeout, payload=payload: RawResponse(
                        payload
                    ),
                )

        with self.assertRaisesRegex(RuntimeError, "unexpected pull request response"):
            create_pull_request(
                repository,
                token="token",
                head="feature",
                base="main",
                title="Title",
                body="",
                draft=False,
                opener=lambda _request, *, timeout: RawResponse(b"[]"),
            )

    def test_create_pull_request_sends_expected_request(self) -> None:
        requests = []

        def opener(request, *, timeout):
            requests.append((request, timeout))
            return Response({"html_url": "https://github.com/acme/project/pull/7"})

        result = create_pull_request(
            GitHubRepository(owner="acme", name="project"),
            token="secret-token",
            head="feature",
            base="main",
            title="Improve things",
            body="Details",
            draft=True,
            opener=opener,
        )

        request, timeout = requests[0]
        self.assertEqual(timeout, 30)
        self.assertEqual(
            request.full_url,
            "https://api.github.com/repos/acme/project/pulls",
        )
        self.assertEqual(
            json.loads(request.data),
            {
                "title": "Improve things",
                "head": "feature",
                "base": "main",
                "body": "Details",
                "draft": True,
            },
        )
        self.assertEqual(result["html_url"], "https://github.com/acme/project/pull/7")
        self.assertNotIn("secret-token", request.data.decode("utf-8"))
        self.assertIn("Bearer secret-token", request.headers["Authorization"])


if __name__ == "__main__":
    unittest.main()
