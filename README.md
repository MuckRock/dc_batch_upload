
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
labelled `name`.  The title is the human readable title of the
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


## Requirements
- System with [Python](https://www.python.org/) and [pip](https://pip.pypa.io/en/stable/installation/) installed. 

- The DocumentCloud Batch Uploader Script. Download the [zip](https://github.com/MuckRock/dc_batch_upload/archive/refs/heads/master.zip) or use the [GitHub CLI](https://github.com/cli/cli#installation) by running `gh repo clone MuckRock/dc_batch_upload` in your terminal/shell/command prompt.

- The project ID of the project you would like to upload the documents to. You can find this project ID by clicking on a project from within DocumentCloud and copying the number after the title of the project and the - in the search bar. 

- The filepath to the directory of documents you would like to upload to DocumentCloud. 
Example: '/home/bob/Documents/bulkupload' or on Windows 'C:\Users\bob\Documents\bulkupload'

- You need to set the environment variables DC_USERNAME (your DocumentCloud username) and DC_PASSWORD (your DocumentCloud password) on your system ([Linux](https://linuxize.com/post/how-to-set-and-list-environment-variables-in-linux/), [Mac OS X](https://phoenixnap.com/kb/set-environment-variable-mac), [Windows](https://phoenixnap.com/kb/windows-set-environment-variable#ftoc-heading-1)). 

- A CSV file with at least two columns: `title` and one other column for the file names, by default it is `document_number` or you can specify a different one using `--id_col your_column_name_here` with the script.
`title` is a human readable title that you would like for the documents, while `document_number` is the actual name of the file on your computer minus the extension. For example, if I have: /home/bob/Documents/bulkupload/test.pdf, `test` would be the document_number. <br />
You may run the following to generate a CSV file for you given a directory of documents if the title is not important for you to configure: <br />
  ```python3 batch_upload.py -p PROJECT_ID --path PATH --csv CSV_NAME --generate_csv``` <br />
  You would then run the following once more to do the upload: <br />
  ```python3 batch_upload.py -p PROJECT_ID --path PATH --csv CSV_NAME``` <br />


