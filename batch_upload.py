#!/usr/bin/env python
"""Script to upload large batches of documents"""

import argparse
import asyncio
import csv
import os
import sqlite3
import time
from queue import Empty, Queue
from threading import Event, Thread, current_thread

import aiohttp
import documentcloud
from documentcloud.exceptions import APIError
from documentcloud.toolbox import grouper
from requests.exceptions import RequestException

SENTINEL = object()


class BatchUploader:
    """Handle uploading large batches of documents"""

    def __init__(self):
        self.headers = None
        self.args = None

    def id_col_index(self):
        return self.headers.index(self.args.id_col)

    def create_db(self):
        """Create a sqlite database to track files that have been uploaded"""
        con = sqlite3.connect(self.args.db_name)
        cur = con.cursor()
        cur.execute(
            "CREATE TABLE documents"
            "(document_number TEXT NOT NULL UNIQUE, uploaded INTEGER NOT NULL, "
            "error INTEGER NOT NULL, error_msg TEXT NOT NULL)"
        )
        con.commit()
        con.close()

    def get_documents_uploaded(self):
        """Read in the document IDs for all documents already uploaded"""
        print("Reading uploaded docs from db")
        uploaded_docs = set()
        con = sqlite3.connect(self.args.db_name)
        cur = con.cursor()
        for row in cur.execute("SELECT document_number FROM documents"):
            uploaded_docs.add(row[0])
        print("Done reading uploaded docs from db", len(uploaded_docs))
        return uploaded_docs

    def get_new_files(self, uploaded_docs):
        """Read files to upload from the CSV"""
        with open(self.args.csv, encoding="utf8") as metadata:
            reader = csv.reader(metadata)
            self.headers = next(reader)
            for row in reader:
                if row[self.id_col_index()] not in uploaded_docs:
                    yield row

    def enqueue_files(self, queue, uploaded_docs, event):
        """Add files to the queue as room becomes available"""
        print("queuing files")
        for i, row in enumerate(self.get_new_files(uploaded_docs)):
            if i % 1000 == 0:
                print(f"queueing file #{i}")
            queue.put(row)
            if self.args.max is not None and i >= (self.args.max - 1):
                break
            if event.is_set():
                break
        print("done queuing")
        queue.put(SENTINEL)

    def row_to_dict(self, row):
        """
        Set the upload paramaters

        Sets projects, source, access and delayed index to the default value for
        all documents.

        Delayed index causes documents to not be indexed immediately, which is much
        more efficient, but may cause a 30 minute delay between uploading the file
        and seeing it in the web view.

        Sets the title based on an exptec title column in the CSV

        Sets the rest of the CSV columns as metadata
        """

        doc_dict = dict(zip(self.headers, row))
        title = doc_dict.pop("title")
        return {
            "title": title,
            "projects": [self.args.project_id],
            "source": self.args.source,
            "access": self.args.access,
            "delayed_index": True,
            "data": doc_dict,
        }

    def get_files_from_queue(self, queue):
        """Get files from the queue and convert them into a format suitable for upload
        Also check if we are out of files to upload
        """
        doc_dicts = []
        finished = False
        for _ in range(self.args.batch_size):
            row = queue.get()
            if row is SENTINEL:
                finished = True
                break
            doc_dict = self.row_to_dict(row)
            doc_dicts.append(doc_dict)
        print(current_thread().name, [d["data"][self.args.id_col] for d in doc_dicts])
        return doc_dicts, finished

    def create_documents(self, client, doc_dicts, con, cur):
        """Create the documents on DocumentCloud"""
        # Upload all the pdfs using the bulk API to reduce the number
        # of API calls and improve performance
        try:
            print(current_thread().name, "create documents")
            response = client.post("documents/", json=doc_dicts)
            response.raise_for_status()
        except (APIError, RequestException) as exc:
            print("create documents exception", str(exc))
            data = [(d["data"][self.args.id_col], 0, 1, str(exc)) for d in doc_dicts]
            print(data)
            cur.executemany(
                "INSERT INTO documents VALUES(?, ?, ? ,?) "
                "ON CONFLICT (document_number) DO "
                "UPDATE SET error=error+1, error_msg=excluded.error_msg",
                data,
            )
            con.commit()
            raise
        for resp, doc_dict in zip(response.json(), doc_dicts):
            doc_dict["id"] = resp["id"]
            doc_dict["presigned_url"] = resp["presigned_url"]

    def upload_files_s3(self, doc_dicts, client, con, cur):
        """Directly upload all of the files to S3"""
        presigned_urls = [d["presigned_url"] for d in doc_dicts]

        async def do_puts():
            tasks = []
            async with aiohttp.ClientSession() as session:
                for url, doc_dict in zip(presigned_urls, doc_dicts):
                    pdf_path = os.path.join(
                        self.args.path,
                        doc_dict["data"][self.args.id_col].lower() + ".pdf",
                    )
                    print(
                        current_thread().name,
                        "uploading",
                        pdf_path,
                        os.path.getsize(pdf_path),
                    )
                    with open(pdf_path, "rb") as pdf_file:
                        tasks.append(session.put(url, data=pdf_file.read()))
                return await asyncio.gather(*tasks, return_exceptions=True)

        responses = asyncio.run(do_puts())
        print(
            "upload response errors",
            [repr(r) for r in responses if isinstance(r, Exception)],
        )
        # get IDs to pass along to process
        process_json = [
            str(d["id"])
            for resp, d in zip(responses, doc_dicts)
            if not isinstance(resp, Exception)
        ]
        # get document numbers of errored documents to mark in db
        error_data = [
            (d["data"][self.args.id_col], 0, 1, str(resp))
            for resp, d in zip(responses, doc_dicts)
            if isinstance(resp, Exception)
        ]
        if error_data:
            print("upload files error", error_data)
            cur.executemany(
                "INSERT INTO documents VALUES(?, ?, ? ,?) "
                "ON CONFLICT (document_number) DO "
                "UPDATE SET error=error+1, error_msg=excluded.error_msg",
                error_data,
            )
            con.commit()
        # get error IDs to delete from DocumentCloud
        error_ids = [
            str(d["id"])
            for resp, d in zip(responses, doc_dicts)
            if isinstance(resp, Exception)
        ]
        if error_ids:
            try:
                client.delete("documents/", params={"id__in": ",".join(error_ids)})
            except (APIError, RequestException) as exc:
                print(f"Error deleting: {exc}")
        return process_json

    def process_documents(self, client, doc_ids, doc_dicts, con, cur):
        """Beging processing the documents after the files have been uploaded"""
        # begin processing the documents
        if not doc_ids:
            return
        try:
            print(current_thread().name, "processing")
            response = client.post("documents/process/", json={"ids": doc_ids})
            response.raise_for_status()
        except (APIError, RequestException) as exc:
            # log all as errors in the db
            print("process error", str(exc))
            data = [
                (d["data"][self.args.id_col], 0, 1, str(exc))
                for d in doc_dicts
                if str(d["id"]) in doc_ids
            ]
            print(data)
            cur.executemany(
                "INSERT INTO documents VALUES(?, ?, ? ,?) "
                "ON CONFLICT (document_number) DO "
                "UPDATE SET error=error+1, error_msg=excluded.error_msg",
                data,
            )
            con.commit()
            # try to delete all of these documents
            try:
                client.delete("documents/", params={"id__in": ",".join(doc_ids)})
            except (APIError, RequestException) as exc_:
                print(f"Error deleting: {exc_}")
            raise

        data = [
            (d["data"][self.args.id_col], 1, 0, "")
            for d in doc_dicts
            if str(d["id"]) in doc_ids
        ]
        print("process success", data)
        cur.executemany(
            "INSERT INTO documents VALUES(?, ?, ? ,?) "
            "ON CONFLICT (document_number) DO "
            "UPDATE SET uploaded=excluded.uploaded",
            data,
        )
        con.commit()

    def drain_queue(self, queue):
        """Empty the queue"""
        try:
            while True:
                queue.get(block=False)
        except Empty:
            pass

    def upload_files_dc(self, queue, client, event):
        """Uploads files to DocumentCloud"""
        con = sqlite3.connect(self.args.db_name)
        cur = con.cursor()
        while True:
            try:
                doc_dicts, finished = self.get_files_from_queue(queue)
                if not doc_dicts:
                    queue.put(SENTINEL)
                    print(current_thread().name, "done")
                    return

                self.create_documents(client, doc_dicts, con, cur)
                process_json = self.upload_files_s3(doc_dicts, client, con, cur)
                self.process_documents(client, process_json, doc_dicts, con, cur)

            except (APIError, RequestException) as exc:
                # if there is an error, first check if we are finished,
                # then continue on to the next batch
                pass
            except Exception as exc:
                # exception catch all
                print("Unknown exception")
                data = [
                    (d["data"][self.args.id_col], 0, 1, str(exc)) for d in doc_dicts
                ]
                print(data)
                cur.executemany(
                    "INSERT INTO documents VALUES(?, ?, ? ,?) "
                    "ON CONFLICT (document_number) DO "
                    "UPDATE SET error=error+1, error_msg=excluded.error_msg",
                    data,
                )
                con.commit()

            if finished or event.is_set():
                con.close()
                if event.is_set():
                    self.drain_queue(queue)
                else:
                    # put the sentinel back on the queue for the other threads to receive it
                    queue.put(SENTINEL)
                print(current_thread().name, "done")
                return

    def delete_proj(self, proj):
        """
        Delete all documents in the project

        You can use this if you start the upload and something goes wrong early on,
        and it'll be easier to just start over.
        """
        client = documentcloud.DocumentCloud(
            username=os.environ["DC_USERNAME"], password=os.environ["DC_PASSWORD"]
        )
        for group in grouper(client.documents.search(f"project:{proj}"), 25):
            ids = [str(d.id) for d in group if d]
            # print(ids)
            if ids:
                resp = client.delete("documents/", params={"id__in": ",".join(ids)})
                resp.raise_for_status()

    def get_error_files(self):
        """Get files which had an error uploading from the database"""
        con = sqlite3.connect(self.args.db_name)
        cur = con.cursor()
        print(
            list(cur.execute("SELECT COUNT(*) FROM documents WHERE uploaded = 0"))[0][0]
        )
        return [
            r[0]
            for r in cur.execute(
                "SELECT document_number FROM documents WHERE uploaded = 0"
            )
        ]

    def reupload_error_files(self):
        """
        Re-upload error files
        Files with errors during upload (error in sqlite db)
        """
        client = documentcloud.DocumentCloud(
            username=os.environ["DC_USERNAME"], password=os.environ["DC_PASSWORD"]
        )
        con = sqlite3.connect(self.args.db_name)
        cur = con.cursor()

        reupload = []

        for document_number in self.get_error_files():
            print("document_number", document_number)
            results = list(
                client.documents.search(
                    "*:*", **{f"data_{self.args.id_col}": document_number}
                )
            )

            if len(results) == 0:
                print("count 0, reupload")
                reupload.append(document_number)
            elif len(results) == 1:
                print("count 1")
                result = results[0]
                if result.status == "success":
                    print("success, set upload true")
                    cur.execute(
                        "UPDATE documents SET uploaded = 1 WHERE document_number = ?",
                        (document_number,),
                    )
                else:
                    print("delete and reupload")
                    try:
                        resp = client.delete(f"documents/{result.id}/")
                        resp.raise_for_status()
                    except (APIError, RequestException) as exc:
                        print(f"error deletiing {result.id}, {exc}")
                        cur.execute(
                            "UPDATE documents SET error = error + 1 WHERE document_number = ?",
                            (document_number,),
                        )
                    else:
                        print("reuploading")
                        reupload.append(document_number)
            else:
                print("count more than 1")
                if any(r.status == "success" for r in results):
                    print("at least one success")
                    first_success = [r for r in results if r.status == "success"][0]
                    for result in results:
                        if result == first_success:
                            continue
                        try:
                            resp = client.delete(f"documents/{result.id}/")
                            resp.raise_for_status()
                        except (APIError, RequestException):
                            print("error deleting")
                            cur.execute(
                                "UPDATE documents SET error = error + 1 WHERE document_number = ?",
                                (document_number,),
                            )
                            break
                    else:
                        print("deleted successfully, setting upload")
                        cur.execute(
                            "UPDATE documents SET uploaded = 1 WHERE document_number = ?",
                            (document_number,),
                        )
                else:
                    for result in results:
                        try:
                            resp = client.delete(f"documents/{result.id}/")
                            resp.raise_for_status()
                        except (APIError, RequestException):
                            cur.execute(
                                "UPDATE documents SET error = error + 1 WHERE document_number = ?",
                                (document_number,),
                            )
                            break
                    else:
                        print("deleted successfully, reuploading")
                        reupload.append(document_number)

            if len(reupload) == self.args.batch_size:
                print("reuploading a batch")
                self.reupload_files(client, con, cur, reupload)
                reupload = []

        # reupload the stragglers
        if reupload:
            self.reupload_files(client, con, cur, reupload)

    def reupload_error_files2(self):
        """
        Re-upload error files
        Files with errors during processing (error on DC)
        """
        client = documentcloud.DocumentCloud(
            username=os.environ["DC_USERNAME"], password=os.environ["DC_PASSWORD"]
        )
        con = sqlite3.connect(self.args.db_name)
        cur = con.cursor()

        reupload = []

        errors = client.documents.search(
            f"+project:{self.args.project_id} +status:(nofile OR error)"
        )
        print(errors.count)

        for result in errors:
            document_number = result.data[self.args.id_col][0]
            print("document_number", document_number)

            print("delete and reupload")
            try:
                resp = client.delete(f"documents/{result.id}/")
                resp.raise_for_status()
            except (APIError, RequestException):
                print("error deletiing")
                cur.execute(
                    "UPDATE documents SET error = error + 1 WHERE document_number = ?",
                    (document_number,),
                )
            else:
                print("reuploading")
                reupload.append(document_number)

            if len(reupload) == self.args.batch_size:
                print("reuploading a batch")
                self.reupload_files(client, con, cur, reupload)
                reupload = []

        # reupload the stragglers
        if reupload:
            self.reupload_files(client, con, cur, reupload)

    def get_rows_from_document_numbers(self, document_numbers):
        """Get the metadata from the CSV given document_numbers"""
        rows = []
        with open(self.args.csv, encoding="utf8") as metadata:
            reader = csv.reader(metadata)
            self.headers = next(reader)
            for row in reader:
                if row[self.id_col_index()] in document_numbers:
                    rows.append(row)
        return rows

    def reupload_files(self, client, con, cur, document_numbers):
        """Re-upload a file which failed"""
        rows = self.get_rows_from_document_numbers(document_numbers)
        doc_dicts = [self.row_to_dict(row) for row in rows]
        if not doc_dicts:
            print("no docs!")
            return

        try:
            self.create_documents(client, doc_dicts, con, cur)
            process_json = self.upload_files_s3(doc_dicts, client, con, cur)
            self.process_documents(client, process_json, doc_dicts, con, cur)
        except (APIError, RequestException) as exc:
            # if there is an error, first check if we are finished,
            # then continue on to the next batch
            pass
        except Exception as exc:
            # exception catch all
            print("Unknown exception")
            data = [(d["data"][self.args.id_col], 0, 1, str(exc)) for d in doc_dicts]
            print(data)
            cur.executemany(
                "INSERT INTO documents VALUES(?, ?, ? ,?) "
                "ON CONFLICT (document_number) DO "
                "UPDATE SET error=error+1, error_msg=excluded.error_msg",
                data,
            )
            con.commit()

    def dedupe(self):
        """
        Deletes duplicate documents

        Should not be needed, but is useful if something goes wrong and you
        accidently upload multiple copies of the same document
        """
        client = documentcloud.DocumentCloud(
            username=os.environ["DC_USERNAME"], password=os.environ["DC_PASSWORD"]
        )
        with open("dupes", encoding="utf8") as dupes:
            for document_number in dupes:
                print()
                results = list(
                    client.documents.search(
                        f"project:{self.args.project_id}",
                        **{f"data_{self.args.id_col}": document_number},
                    )
                )
                if any(r.status == "success" for r in results):
                    print("at least one success")
                    first_success = [r for r in results if r.status == "success"][0]
                    for result in results:
                        if result == first_success:
                            continue
                        try:
                            resp = client.delete(f"documents/{result.id}/")
                            resp.raise_for_status()
                        except (APIError, RequestException):
                            print(f"error deleting {document_number}")
                            break
                    else:
                        print("deleted successfully")
                else:
                    print(f"no success {document_number}")

    def main(self):
        """Entry point"""

        start = time.time()

        self.parse_arguments()

        if not os.path.exists(self.args.db_name):
            self.create_db()

        queue = Queue(maxsize=self.args.num_threads * self.args.batch_size * 2)
        client = documentcloud.DocumentCloud(
            username=os.environ["DC_USERNAME"], password=os.environ["DC_PASSWORD"]
        )
        event = Event()

        uploaded_docs = self.get_documents_uploaded()
        enqueue_thread = Thread(
            target=self.enqueue_files, args=(queue, uploaded_docs, event)
        )
        enqueue_thread.start()
        upload_threads = [
            Thread(target=self.upload_files_dc, args=(queue, client, event))
            for _ in range(self.args.num_threads)
        ]
        try:
            for thread in upload_threads:
                thread.start()
            enqueue_thread.join()
            for thread in upload_threads:
                thread.join()
        except KeyboardInterrupt:
            print("CTRL-C detected, shutting down gracefully")
            event.set()
            enqueue_thread.join()
            for thread in upload_threads:
                thread.join()

        end = time.time()

        print("All done!", end - start, "seconds", self.args.num_threads, "threads")

    def parse_arguments(self):
        """Get argument data"""
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "-p",
            "--project_id",
            type=int,
            required=True,
            help="The ID of the project to upload documents to",
        )
        parser.add_argument(
            "--path", required=True, help="Path to the directory where the PDFs are"
        )
        parser.add_argument(
            "--csv",
            required=True,
            help="Path to a CSV file containing the metadata for the PDFs",
        )
        parser.add_argument(
            "--id_col",
            default="document_number",
            help="Name of column containing a unique identifier for the document "
            "(default: document_number)",
        )
        parser.add_argument(
            "--num_threads",
            type=int,
            choices=range(1, 5),
            default=1,
            help="Number of threads (default: 1)",
        )
        parser.add_argument(
            "--batch_size",
            type=int,
            choices=range(1, 26),
            default=25,
            help="Number of documents to process at once (default: 25)",
        )
        parser.add_argument(
            "--max", type=int, help="Maximum number of documents to upload"
        )
        parser.add_argument(
            "--db_name",
            default="batch.db",
            help="The name of the sqlite database to log to (default: batch.db)",
        )
        parser.add_argument(
            "--access",
            default="private",
            help="The access level to upload documents as (default: private)",
        )
        parser.add_argument(
            "--source", default="", help="Set the source for the uploaded documents"
        )
        self.args = parser.parse_args()


if __name__ == "__main__":
    BatchUploader().main()
