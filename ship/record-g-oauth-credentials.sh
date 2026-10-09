read -r -p 'Google web OAuth client ID: ' FL_INPUT
printf '%s\n' "$FL_INPUT" > "$FL_STATE/google-client-id"
read -r -s -p 'Google web OAuth client secret: ' FL_INPUT
printf '\n'
printf '%s\n' "$FL_INPUT" > "$FL_STATE/google-client-secret"
unset FL_INPUT
