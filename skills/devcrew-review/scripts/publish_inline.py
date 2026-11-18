"""Publish one explicitly requested inline comment with version and position checks."""

import argparse
import json
import pathlib
import re
import subprocess
import sys
from urllib.parse import quote, unquote, urlsplit


class PublicationError(ValueError):
    pass


def target(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise PublicationError("expected an HTTPS pull or merge request URL without credentials")
    path = unquote(parsed.path).strip("/")
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise PublicationError("invalid project path")
    github = re.fullmatch(r"([\w.-]+)/([\w.-]+)/pull/([1-9][0-9]*)(?:/files)?", path)
    if github:
        owner, repo, number = github.groups()
        project = f"repos/{owner}/{repo}"
        return "gh", parsed.netloc, project, f"{project}/pulls/{number}"
    gitlab = re.fullmatch(r"([\w./-]+)/-/merge_requests/([1-9][0-9]*)(?:/diffs)?", path)
    if gitlab:
        project, number = gitlab.groups()
        return (
            "glab",
            parsed.netloc,
            project,
            f"projects/{quote(project, safe='')}/merge_requests/{number}",
        )
    raise PublicationError("expected a GitHub pull request or GitLab merge request URL")


def api(tool, host, endpoint, payload=None):
    argv = [tool, "api", "--hostname", host]
    data = None
    if payload is not None:
        argv.extend(
            ["--method", "POST", "--header", "Content-Type: application/json", "--input", "-"]
        )
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    result = subprocess.run(
        [*argv, endpoint], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if result.returncode:
        raise PublicationError(f"{tool} api exited with status {result.returncode}")
    return json.loads(result.stdout)


def publish(args, body):
    tool, host, project, request = target(args.url)
    if not body.strip() or not args.path or not (args.new_line or args.old_line):
        raise PublicationError("supply a nonempty body, path and reviewed line")
    if any(n is not None and n < 1 for n in (args.old_line, args.new_line)):
        raise PublicationError("line numbers must be positive")
    if any(
        not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha)
        for sha in (args.base_sha, args.head_sha)
    ):
        raise PublicationError("supply the full reviewed commit SHAs")

    if tool == "gh":
        pr = api(tool, host, request)
        if (pr["base"]["sha"], pr["head"]["sha"]) != (args.base_sha, args.head_sha):
            raise PublicationError("diff version changed; review the current base and head")
        position = {
            "commit_id": args.head_sha,
            "path": args.path,
            "side": "RIGHT" if args.new_line else "LEFT",
            "line": args.new_line or args.old_line,
        }
        payload = {"body": body, **position}
        create_path = request + "/comments"
        read_path = f"{project}/pulls/comments"
    else:
        if not args.start_sha or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", args.start_sha):
            raise PublicationError("GitLab requires the reviewed start SHA")
        versions = api(tool, host, request + "/versions")
        expected = {
            "base_sha": args.base_sha,
            "start_sha": args.start_sha,
            "head_sha": args.head_sha,
        }
        if not versions or any(
            versions[0][k.replace("_sha", "_commit_sha")] != v for k, v in expected.items()
        ):
            raise PublicationError("diff version changed; review the current version")
        position = {
            "position_type": "text",
            "old_path": args.old_path or args.path,
            "new_path": args.path,
            **expected,
        }
        for side, line in (("old_line", args.old_line), ("new_line", args.new_line)):
            if line is not None:
                position[side] = line
        payload = {"body": body, "position": position}
        create_path = read_path = request + "/discussions"

    try:
        created = api(tool, host, create_path, payload)
        comment_id = created["id"]
        if type(comment_id) not in (str, int) or not re.fullmatch(
            r"[A-Za-z0-9_-]+", str(comment_id)
        ):
            raise ValueError("invalid publication ID")
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise PublicationError(
            "publication outcome unknown; inspect existing comments before retrying"
        ) from error
    try:
        item = api(tool, host, f"{read_path}/{comment_id}")
        if tool == "gh":
            saved = item
        else:
            saved = item["notes"][0]
        actual = saved if tool == "gh" else saved["position"]
        verified = (
            str(item["id"]) == str(comment_id)
            and saved["body"] == body
            and all(actual.get(key) == value for key, value in position.items())
        )
        if tool == "glab":
            verified = (
                verified
                and actual.get("old_line") == args.old_line
                and actual.get("new_line") == args.new_line
            )
        if not verified:
            raise ValueError("comment or position differs")
    except (ValueError, KeyError, TypeError, IndexError, OSError) as error:
        raise PublicationError(
            f"created {comment_id} but readback failed; inspect this ID before retrying"
        ) from error
    return {"status": "verified", "url": args.url, "id": comment_id, "position": position}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("url", "path", "base-sha", "head-sha"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--body-file", type=pathlib.Path, required=True, help="UTF-8 comment text")
    parser.add_argument("--start-sha", help="GitLab diff start SHA")
    parser.add_argument("--old-path", help="original GitLab filename for a rename")
    parser.add_argument("--old-line", type=int)
    parser.add_argument("--new-line", type=int)
    args = parser.parse_args()
    try:
        result = publish(args, args.body_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
