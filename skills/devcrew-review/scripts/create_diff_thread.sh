#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  create_diff_thread.sh --mr URL --body TEXT --new-path PATH --old-path PATH \
    --base-sha SHA --start-sha SHA --head-sha SHA \
    [--new-line N] [--old-line N]

Pass --new-line for an addition, --old-line for a deletion,
and both for a context line.
SHAs must identify the reviewed diff version.
EOF
}

die() {
  printf 'Error: %s\n' "$1" >&2
  exit 1
}

require_value() {
  [[ $# -ge 2 && -n "$2" ]] || die "missing value for $1"
}

mr_url=""
body=""
new_path=""
old_path=""
new_line=""
old_line=""
expected_base_sha=""
expected_start_sha=""
expected_head_sha=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mr)
      require_value "$@"
      mr_url="$2"
      shift 2
      ;;
    --body)
      require_value "$@"
      body="$2"
      shift 2
      ;;
    --new-path)
      require_value "$@"
      new_path="$2"
      shift 2
      ;;
    --old-path)
      require_value "$@"
      old_path="$2"
      shift 2
      ;;
    --new-line)
      require_value "$@"
      new_line="$2"
      shift 2
      ;;
    --old-line)
      require_value "$@"
      old_line="$2"
      shift 2
      ;;
    --base-sha)
      require_value "$@"
      expected_base_sha="$2"
      shift 2
      ;;
    --start-sha)
      require_value "$@"
      expected_start_sha="$2"
      shift 2
      ;;
    --head-sha)
      require_value "$@"
      expected_head_sha="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "unknown argument: $1"
      ;;
  esac
done

command -v glab >/dev/null 2>&1 || die "glab not found"
command -v jq >/dev/null 2>&1 || die "jq not found"

[[ -n "$mr_url" ]] || die "missing merge request URL"
[[ -n "$body" ]] || die "empty comment body"
[[ -n "$new_path" ]] || die "missing new file path"
[[ -n "$old_path" ]] || die "missing old file path"
[[ -n "$new_line" || -n "$old_line" ]] || die "provide --new-line or --old-line"
[[ -n "$expected_base_sha" && -n "$expected_start_sha" && -n "$expected_head_sha" ]] \
  || die "provide --base-sha, --start-sha and --head-sha for the reviewed diff"

if [[ -n "$new_line" && ! "$new_line" =~ ^[1-9][0-9]*$ ]]; then
  die "--new-line must be a positive integer"
fi
if [[ -n "$old_line" && ! "$old_line" =~ ^[1-9][0-9]*$ ]]; then
  die "--old-line must be a positive integer"
fi

clean_url="${mr_url%%#*}"
clean_url="${clean_url%%\?*}"
clean_url="${clean_url%/}"

if [[ "$clean_url" =~ ^https?://([^/]+)/(.+)/-/merge_requests/([0-9]+)(/.*)?$ ]]; then
  host="${BASH_REMATCH[1]}"
  project_path="${BASH_REMATCH[2]}"
  mr_iid="${BASH_REMATCH[3]}"
else
  die "invalid GitLab merge request URL"
fi

glab auth status --hostname "$host" >/dev/null 2>&1 \
  || die "glab is not authenticated for $host"

project_id="$(jq -rn --arg value "$project_path" '$value | @uri')"
mr_endpoint="projects/${project_id}/merge_requests/${mr_iid}"

versions_json="$(glab api --hostname "$host" "${mr_endpoint}/versions")" \
  || die "could not read diff versions"

base_sha="$(jq -er '.[0].base_commit_sha | select(type == "string" and length > 0)' <<<"$versions_json")" \
  || die "latest diff version has no base_commit_sha"
start_sha="$(jq -er '.[0].start_commit_sha | select(type == "string" and length > 0)' <<<"$versions_json")" \
  || die "latest diff version has no start_commit_sha"
head_sha="$(jq -er '.[0].head_commit_sha | select(type == "string" and length > 0)' <<<"$versions_json")" \
  || die "latest diff version has no head_commit_sha"

[[ "$base_sha" == "$expected_base_sha" && "$start_sha" == "$expected_start_sha" && "$head_sha" == "$expected_head_sha" ]] \
  || die "diff version changed; recheck the finding, paths and lines before posting"

new_line_json="null"
old_line_json="null"
[[ -n "$new_line" ]] && new_line_json="$new_line"
[[ -n "$old_line" ]] && old_line_json="$old_line"

payload="$(jq -n \
  --arg body "$body" \
  --arg base_sha "$base_sha" \
  --arg start_sha "$start_sha" \
  --arg head_sha "$head_sha" \
  --arg new_path "$new_path" \
  --arg old_path "$old_path" \
  --argjson new_line "$new_line_json" \
  --argjson old_line "$old_line_json" \
  '{
    body: $body,
    position: {
      position_type: "text",
      base_sha: $base_sha,
      start_sha: $start_sha,
      head_sha: $head_sha,
      new_path: $new_path,
      old_path: $old_path
    }
  }
  | if $new_line != null then .position.new_line = $new_line else . end
  | if $old_line != null then .position.old_line = $old_line else . end')"

created_json="$(printf '%s\n' "$payload" \
  | glab api --hostname "$host" --method POST \
      --header "Content-Type: application/json" \
      --input - \
      "${mr_endpoint}/discussions")" \
  || die "GitLab did not create the discussion"

discussion_id="$(jq -er '.id | select(type == "string" and length > 0)' <<<"$created_json")" \
  || die "GitLab created the discussion without returning an ID"

verified_json="$(glab api --hostname "$host" "${mr_endpoint}/discussions/${discussion_id}")" \
  || die "discussion ${discussion_id} was created but could not be read back"

if ! jq -e \
  --arg base_sha "$expected_base_sha" \
  --arg start_sha "$expected_start_sha" \
  --arg head_sha "$expected_head_sha" \
  --arg new_path "$new_path" \
  --arg old_path "$old_path" \
  --argjson new_line "$new_line_json" \
  --argjson old_line "$old_line_json" \
  '.notes[0].position as $position
   | ($position != null)
     and ($position.position_type == "text")
     and ($position.base_sha == $base_sha)
     and ($position.start_sha == $start_sha)
     and ($position.head_sha == $head_sha)
     and ($position.new_path == $new_path)
     and ($position.old_path == $old_path)
     and (($new_line == null and $position.new_line == null) or $position.new_line == $new_line)
     and (($old_line == null and $position.old_line == null) or $position.old_line == $old_line)' \
  <<<"$verified_json" >/dev/null; then
  die "discussion ${discussion_id} was created but its inline position could not be verified"
fi

jq -n \
  --arg status "verified" \
  --arg mr "$mr_url" \
  --arg discussion_id "$discussion_id" \
  --argjson note_id "$(jq -e '.notes[0].id' <<<"$verified_json")" \
  --argjson position "$(jq -c '.notes[0].position' <<<"$verified_json")" \
  '{
    status: $status,
    merge_request: $mr,
    discussion_id: $discussion_id,
    note_id: $note_id,
    position: $position
  }'
