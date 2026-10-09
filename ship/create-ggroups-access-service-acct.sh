gcloud auth login
gcloud services enable cloudidentity.googleapis.com --project="$FL_GOOGLE_PROJECT"
gcloud iam service-accounts create fl-group-reader --project="$FL_GOOGLE_PROJECT" \
  --display-name='Fletcherlake group reader'
gcloud iam service-accounts keys create "$FL_STATE/google-groups.json" \
  --project="$FL_GOOGLE_PROJECT" \
  --iam-account="fl-group-reader@$FL_GOOGLE_PROJECT.iam.gserviceaccount.com"
chmod 600 "$FL_STATE/google-groups.json"
