import os
import time
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv
from pymongo import MongoClient
from bson import ObjectId

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


class SlateSerializer:
    """Converts a Slate AST (list of nodes) into an HTML string."""

    BLOCK_TAGS = {
        "paragraph": "p",
        "h1": "h1",
        "h2": "h2",
        "ul": "ul",
        "li": "li",
    }

    def to_html(self, nodes: list) -> str:
        html = ""
        for node in nodes or []:
            node_type = node.get("type")

            if node_type in self.BLOCK_TAGS:
                tag = self.BLOCK_TAGS[node_type]
                inner = self.to_html(node.get("children", []))
                html += f"<{tag}>{inner}</{tag}>"

            elif node_type == "link":
                url = node.get("url", "#")
                inner = self.to_html(node.get("children", []))
                html += f'<a href="{url}">{inner}</a>'

            elif "text" in node:
                text = node["text"]
                if node.get("bold"):
                    text = f"<strong>{text}</strong>"
                if node.get("italic"):
                    text = f"<em>{text}</em>"
                html += text

            else:
                html += self.to_html(node.get("children", []))

        return html


class EmailSender:
    """Builds and dispatches MIME emails via SMTP."""

    def __init__(self, host: str, port: int, from_address: str):
        self.host = host
        self.port = port
        self.from_address = from_address

    def send(
        self,
        to_addresses: list[str],
        subject: str,
        html: str,
        cc_addresses: list[str] | None = None,
        bcc_addresses: list[str] | None = None,
    ) -> None:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.from_address
        msg["To"] = ", ".join(to_addresses)
        if cc_addresses:
            msg["Cc"] = ", ".join(cc_addresses)
        msg.attach(MIMEText(html, "html"))

        all_recipients = to_addresses + (cc_addresses or []) + (bcc_addresses or [])
        with smtplib.SMTP(self.host, self.port) as server:
            server.sendmail(self.from_address, all_recipients, msg.as_string())


class CommunicationWorker:
    """Polls MongoDB for pending communications and processes them."""

    def __init__(self, db, email_sender: EmailSender, serializer: SlateSerializer, poll_interval: int):
        self.db = db
        self.email_sender = email_sender
        self.serializer = serializer
        self.poll_interval = poll_interval

    def _resolve_emails(self, relationship_list: list) -> list[str]:
        """Resolve Payload relationship references to email addresses."""
        if not relationship_list:
            return []
        ids = [ObjectId(r["value"]) for r in relationship_list if r.get("value")]
        users = self.db.users.find({"_id": {"$in": ids}}, {"email": 1})
        return [u["email"] for u in users if u.get("email")]

    def _set_status(self, doc_id, status: str) -> None:
        self.db.communications.update_one({"_id": doc_id}, {"$set": {"status": status}})

    def process(self, doc: dict) -> None:
        doc_id = doc["_id"]
        log.info(f"Processing communication {doc_id}")
        self._set_status(doc_id, "processing")

        try:
            to_emails = self._resolve_emails(doc.get("tos") or [])
            if not to_emails:
                raise ValueError("No valid 'to' email addresses found")

            cc_emails = self._resolve_emails(doc.get("ccs") or [])
            bcc_emails = self._resolve_emails(doc.get("bccs") or [])
            html = self.serializer.to_html(doc.get("body") or [])

            self.email_sender.send(to_emails, doc["subject"], html, cc_emails, bcc_emails)
            self._set_status(doc_id, "sent")
            log.info(f"Communication {doc_id} sent successfully")

        except Exception as e:
            log.error(f"Failed to process communication {doc_id}: {e}")
            self._set_status(doc_id, "failed")

    def run(self) -> None:
        log.info(f"Worker started. Polling every {self.poll_interval}s")
        while True:
            doc = self.db.communications.find_one({"status": "pending"})
            if doc:
                self.process(doc)
            else:
                time.sleep(self.poll_interval)


if __name__ == "__main__":
    _client = MongoClient(os.environ["MONGODB_URI"])
    _db = _client.get_default_database()

    _email_sender = EmailSender(
        host=os.getenv("SMTP_HOST", "localhost"),
        port=int(os.getenv("SMTP_PORT", 1025)),
        from_address=os.getenv("EMAIL_FROM", "worker@mzinga.io"),
    )

    _worker = CommunicationWorker(
        db=_db,
        email_sender=_email_sender,
        serializer=SlateSerializer(),
        poll_interval=int(os.getenv("POLL_INTERVAL_SECONDS", 5)),
    )

    _worker.run()