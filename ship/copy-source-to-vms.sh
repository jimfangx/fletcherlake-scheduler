# actually upload the fletcherlake repo code to the 2 vms
git cat-file -e "$FL_REPO_REV^{commit}"
git archive --format=tar "$FL_REPO_REV" > "$FL_STATE/source.tar"
flsave
for FL_ROLE in scheduler gateway; do
  flssh "$FL_ROLE" 'install -d -m 700 ~/fl-secrets'
  flput "$FL_ROLE" "$FL_STATE/settings.env" fl-secrets/settings.env
  flput "$FL_ROLE" "$FL_STATE/resources.env" fl-secrets/resources.env
  flput "$FL_ROLE" "$FL_STATE/source.tar" fl-secrets/source.tar
done
