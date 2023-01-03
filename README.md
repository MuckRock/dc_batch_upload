
# DocumentCloud Batch Uploader Script

This script was written to upload the CIA Crest files, which contains almost 1
million files.  It keeps track of which files were uploaded succesfully, so
that it can be stopped and restarted and it will pick up where it left off, and
errors can be retried.  It uploads files in batches.  It can be stopped
gracefully by pressing Ctrl+C (once) while it is running.

The Crest documents contain a CSV of all the documents and metadata.  The
metadata contains the document's title, and the rest is uploaded as key-value
data on the document.  If you do not have a CSV file of all of your documents,
you will need to create one.  It must contain a title column and a column which
contains the file name (without the trailing .pdf), which by default is
labelled `document_number`.  The title is the human readable title of the
document.  The script will attempt to open the file by joining the given PDF
path with the name from the file name column and appending .pdf.  For example.
given a PDF path of `/data/files/` and a file name of `document`, the file will
be expected to be named `/data/files/document.pdf`.

You can set the project ID, PDF path, CSV path, default access, and default
source using the command line arguments.  Use the `--help` argument for more
details.  You also must set your DocumentCloud username and password in the
environment variables, `DC_USERNAME` and `DC_PASSWORD`, respectively.

After all files have been attempted to be uploaded, you can manually run
`reupload_error_files` and `reupload_error_files2`, which will attempt to
re-upload files which had errors.  You may need to run them more than once.  If
you have some large files, you may need to reduce the batch size to succesfully
upload them.  Files larger than 500MB are never accepted.


