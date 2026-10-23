#!/usr/bin/env bash
# Mint the GitHub App tokens for one Sadr job, and keep them fresh.
#
#   app-token.sh APP_ID OWNER NAME [READ_REPO...] < private-key.pem
#
# Mints a write token for OWNER/NAME and a read-only token for the
# READ_REPOs (repo names in OWNER). Writes them to $RUNNER_TEMP/sadr-token,
# and installs:
#   - a git credential helper: the write token for OWNER/NAME, the read
#     token for every other github.com repo
#   - a gh wrapper that always uses the current write token
#   - `sadr-token write|read`, which prints the current token
# Then it starts a background loop that mints new tokens every 45 minutes,
# because an App token lasts one hour and a Sadr job can run six.
#
# The private key comes on stdin and stays in this process's memory. It is
# never exported or written to disk. Outputs (in $GITHUB_OUTPUT): app-token,
# read-token, app-slug, bot-name (the bot's login), bot-id, bot-email.
#
# Local commits use the bot user's login and noreply email. commit.py
# publishes them through the API, where GitHub signs a commit only under the
# App's own identity, so the published author is the same bot user. Each
# owner's App has its own slug and bot id, so both are looked up here.
set -euo pipefail

app_id=$1 owner=$2 name=$3
shift 3
read_repos=("$@")
key=$(cat)
api=https://api.github.com
dir="$RUNNER_TEMP/sadr-token"
write_perms='{"contents":"write","pull_requests":"write","issues":"write","workflows":"write","checks":"read","statuses":"read","metadata":"read"}'
read_perms='{"contents":"read","metadata":"read"}'

umask 077
mkdir -p "$dir/bin"

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

jwt() {
  local now header payload sig
  now=$(date +%s)
  header=$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)
  payload=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' $((now - 60)) $((now + 540)) "$app_id" | b64url)
  sig=$(printf '%s.%s' "$header" "$payload" | openssl dgst -sha256 -sign <(printf '%s\n' "$key") | b64url)
  printf '%s.%s.%s' "$header" "$payload" "$sig"
}

app_api() { # METHOD PATH [BODY]
  curl -fsS --retry 3 -X "$1" \
    -H "Authorization: Bearer $(jwt)" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    ${3:+--data "$3"} "$api$2"
}

repo_lower=$(printf %s "$owner/$name" | tr "[:upper:]" "[:lower:]")
installation=$(app_api GET "/repos/$owner/$name/installation" | jq -r .id)

mint() { # FILE PERMISSIONS REPO...
  local file=$1 perms=$2 body token
  shift 2
  body=$(jq -cn --argjson p "$perms" '{permissions: $p, repositories: $ARGS.positional}' --args "$@")
  token=$(app_api POST "/app/installations/$installation/access_tokens" "$body" | jq -r .token)
  [ -n "$token" ] && [ "$token" != null ] || return 1
  echo "::add-mask::$token"
  printf '%s' "$token" > "$file.new"
  mv "$file.new" "$file"
}

mint_all() {
  mint "$dir/app" "$write_perms" "$name"
  if [ ${#read_repos[@]} -gt 0 ]; then
    # A repo the installation cannot reach makes the whole request fail.
    # Sadr then reports the related repos as unreachable.
    mint "$dir/read" "$read_perms" "${read_repos[@]}" || {
      echo "::warning::Could not mint the read token for: ${read_repos[*]}"
      : > "$dir/read"
    }
  else
    : > "$dir/read"
  fi
}

mint_all

# git: the write token for this repo, the read token for any other repo.
cat > "$dir/bin/git-credential-sadr" <<EOF
#!/usr/bin/env bash
[ "\$1" = get ] || exit 0
path=
while IFS='=' read -r k v; do
  [ -z "\$k" ] && break
  [ "\$k" = path ] && path=\$v
done
path=\$(printf %s "\${path%.git}" | tr "[:upper:]" "[:lower:]")
if [ "\$path" = "$repo_lower" ]; then f=app; else f=read; fi
token=\$(cat "$dir/\$f")
[ -n "\$token" ] || exit 0
printf 'username=x-access-token\npassword=%s\n' "\$token"
EOF

real_gh=$(command -v gh)
cat > "$dir/bin/gh" <<EOF
#!/usr/bin/env bash
GH_TOKEN=\$(cat "$dir/app")
export GH_TOKEN
exec "$real_gh" "\$@"
EOF

cat > "$dir/bin/sadr-token" <<EOF
#!/usr/bin/env bash
case "\${1:-}" in
  write) cat "$dir/app" ;;
  read) cat "$dir/read" ;;
  *) echo "usage: sadr-token write|read" >&2; exit 2 ;;
esac
EOF
chmod 700 "$dir/bin/git-credential-sadr" "$dir/bin/gh" "$dir/bin/sadr-token"

git config --global credential.https://github.com.useHttpPath true
git config --global --replace-all credential.https://github.com.helper ''
git config --global --add credential.https://github.com.helper "$dir/bin/git-credential-sadr"
echo "$dir/bin" >> "$GITHUB_PATH"

# Commit locally as the App's bot user, the author that GitHub shows.
slug=$(app_api GET /app | jq -r .slug)
bot_name="$slug[bot]"
bot_id=$(curl -fsS --retry 3 -H "Authorization: Bearer $(cat "$dir/app")" "$api/users/$slug%5Bbot%5D" | jq -r .id)
[[ "$bot_id" =~ ^[0-9]+$ ]] || { echo "::error::Could not look up the bot user $bot_name."; exit 1; }
bot_email="$bot_id+$bot_name@users.noreply.github.com"
git config --global user.name "$bot_name"
git config --global user.email "$bot_email"

{
  echo "app-token=$(cat "$dir/app")"
  echo "read-token=$(cat "$dir/read")"
  echo "app-slug=$slug"
  echo "bot-name=$bot_name"
  echo "bot-id=$bot_id"
  echo "bot-email=$bot_email"
} >> "$GITHUB_OUTPUT"

# Keep the tokens fresh until the job ends. The runner stops this loop then.
(
  while sleep 2700; do
    mint_all || true
  done
) </dev/null >/dev/null 2>&1 &
disown
