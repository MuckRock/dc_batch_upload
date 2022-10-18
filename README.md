
# DocumentCloud Batch Uploader Script

This script was written to upload the CIA Crest files, which contains almost 1
million files.  It keeps track of which files were uploaded succesfully, so
that it can be stopped and restarted and it will pick up where it left off, and
errors can be retried.  It uploads files in batches.  It can be stopped
gracefully by pressing Ctrl+C while it is running.

The Crest documents contain a CSV of all the documents and metadata.  The
metadata contains the document's title, and the rest is uploaded as key-value
data on the document.  If you do not have a CSV file of all of your documents,
you will need to either create one or modify the script to run through all
documents in a directory.

You need to set the project ID, PDF path, CSV path, default access, and default
source at the top of the script before running it.

After all files have been attempted to be uploaded, you can manually run
`reupload_error_files` and `reupload_error_files2`, which will attempt to
re-upload files which had errors.  You may need to run them more than once.  If
you have some large files, you may need to reduce the batch size to succesfully
upload them.  Files larger than 500MB will not be accepted.


