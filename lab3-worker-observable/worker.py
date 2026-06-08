import os
import time
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import requests
from dotenv import load_dotenv

import structlog
from opentelemetry import trace, metrics
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from prometheus_client import start_http_server
from opentelemetry.trace.status import Status, StatusCode

load_dotenv()

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", 5))
SMTP_HOST = os.getenv("SMTP_HOST", "localhost")
SMTP_PORT = int(os.getenv("SMTP_PORT", 1025))
EMAIL_FROM = os.getenv("EMAIL_FROM", "worker@mzinga.io")
API_BASE_URL = os.getenv("MZINGA_URL", "http://localhost:3000")
ADMIN_EMAIL = os.getenv("MZINGA_EMAIL")
ADMIN_PASSWORD = os.getenv("MZINGA_PASSWORD")
OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318/v1/traces")
OTEL_SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "email-worker")
PROMETHEUS_PORT = int(os.getenv("PROMETHEUS_PORT", 8000))

def add_otel_context(logger, log_method, event_dict):
    span = trace.get_current_span()
    if span.is_recording():
        ctx = span.get_span_context()
        event_dict["trace_id"] = trace.format_trace_id(ctx.trace_id)
        event_dict["span_id"] = trace.format_span_id(ctx.span_id)
    return event_dict

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        add_otel_context,
        structlog.processors.JSONRenderer()
    ],
    wrapper_class=structlog.make_filtering_bound_logger(20),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True
)
log = structlog.get_logger(service=OTEL_SERVICE_NAME)

resource = Resource.create({
    "service.name": OTEL_SERVICE_NAME,
    "service.version": "1.0.0"
})

tracer_provider = TracerProvider(resource=resource)
otlp_exporter = OTLPSpanExporter(endpoint=OTEL_EXPORTER_OTLP_ENDPOINT)
span_processor = BatchSpanProcessor(otlp_exporter)
tracer_provider.add_span_processor(span_processor)
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer(__name__)

RequestsInstrumentor().instrument()

start_http_server(port=PROMETHEUS_PORT, addr="0.0.0.0")
metric_reader = PrometheusMetricReader()
meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter(__name__)

emails_processed_total = meter.create_counter("emails_processed_total")
email_processing_duration_seconds = meter.create_histogram("email_processing_duration_seconds")
smtp_send_duration_seconds = meter.create_histogram("smtp_send_duration_seconds")
worker_poll_total = meter.create_counter("worker_poll_total")

class MzingaAPIClient:
    def __init__(self):
        self.token = None

    def login(self):
        url = f"{API_BASE_URL}/api/users/login"
        payload = {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
        response = requests.post(url, json=payload)
        response.raise_for_status()
        self.token = response.json().get("token")

    def request(self, method, endpoint, payload=None):
        if not self.token:
            self.login()
        
        url = f"{API_BASE_URL}{endpoint}"
        headers = {"Authorization": f"Bearer {self.token}"}
        
        response = requests.request(method, url, json=payload, headers=headers)
        
        if response.status_code == 401:
            self.login()
            headers["Authorization"] = f"Bearer {self.token}"
            response = requests.request(method, url, json=payload, headers=headers)
            
        response.raise_for_status()
        return response.json()

class EmailService:
    def send_email(self, to_addresses, subject, html, cc_addresses=None, bcc_addresses=None):
        with tracer.start_as_current_span("send_email") as span:
            start_time = time.time()
            cc_list = cc_addresses or []
            bcc_list = bcc_addresses or []
            recipient_count = len(to_addresses) + len(cc_list) + len(bcc_list)
            span.set_attribute("recipient_count", recipient_count)
            
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = EMAIL_FROM
            msg["To"] = ", ".join(to_addresses)
            if cc_list:
                msg["Cc"] = ", ".join(cc_list)
            
            msg.attach(MIMEText(html, "html"))
            all_recipients = to_addresses + cc_list + bcc_list
            
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.sendmail(EMAIL_FROM, all_recipients, msg.as_string())
                
            duration = time.time() - start_time
            smtp_send_duration_seconds.record(duration)

def slate_to_html(nodes):
    with tracer.start_as_current_span("serialize_body") as span:
        span.set_attribute("node_count", len(nodes) if nodes else 0)
        return _slate_to_html_recursive(nodes)

def _slate_to_html_recursive(nodes):
    html = ""
    for node in nodes or []:
        if node.get("type") == "paragraph":
            html += f"<p>{_slate_to_html_recursive(node.get('children', []))}</p>"
        elif node.get("type") == "h1":
            html += f"<h1>{_slate_to_html_recursive(node.get('children', []))}</h1>"
        elif node.get("type") == "h2":
            html += f"<h2>{_slate_to_html_recursive(node.get('children', []))}</h2>"
        elif node.get("type") == "ul":
            html += f"<ul>{_slate_to_html_recursive(node.get('children', []))}</ul>"
        elif node.get("type") == "li":
            html += f"<li>{_slate_to_html_recursive(node.get('children', []))}</li>"
        elif node.get("type") == "link":
            url = node.get("url", "#")
            html += f'<a href="{url}">{_slate_to_html_recursive(node.get("children", []))}</a>'
        elif "text" in node:
            text = node["text"]
            if node.get("bold"):
                text = f"<strong>{text}</strong>"
            if node.get("italic"):
                text = f"<em>{text}</em>"
            html += text
        else:
            html += _slate_to_html_recursive(node.get("children", []))
    return html

def extract_emails(relationships):
    if not relationships:
        return []
    return [r["value"]["email"] for r in relationships if r.get("value") and r["value"].get("email")]

class CommunicationsWorker:
    def __init__(self):
        self.api = MzingaAPIClient()
        self.mailer = EmailService()

    def start(self):
        log.info("Worker REST started", poll_interval=POLL_INTERVAL)
        while True:
            try:
                response = self.api.request("GET", "/api/communications?where[status][equals]=pending&depth=1")
                docs = response.get("docs", [])
                
                if not docs:
                    worker_poll_total.add(1, {"result": "empty"})
                    time.sleep(POLL_INTERVAL)
                    continue
                    
                worker_poll_total.add(1, {"result": "found"})
                for doc in docs:
                    self.process_document(doc)
            except Exception as e:
                log.error("Polling error", error=str(e))
                time.sleep(POLL_INTERVAL)

    def process_document(self, doc):
        doc_id = doc["id"]
        structlog.contextvars.bind_contextvars(doc_id=doc_id)
        start_time = time.time()
        
        with tracer.start_as_current_span("process_communication") as span:
            span.set_attribute("doc_id", doc_id)
            log.info("Processing communication")
            status = "failed"
            recipient_count = 0
            
            try:
                self.api.request("PATCH", f"/api/communications/{doc_id}", payload={"status": "processing"})
                
                to_emails = extract_emails(doc.get("tos"))
                cc_emails = extract_emails(doc.get("ccs"))
                bcc_emails = extract_emails(doc.get("bccs"))
                
                recipient_count = len(to_emails) + len(cc_emails) + len(bcc_emails)
                
                if not to_emails:
                    raise ValueError("No valid 'to' email addresses found")
                    
                html_body = slate_to_html(doc.get("body"))
                
                self.mailer.send_email(to_emails, doc.get("subject", ""), html_body, cc_emails, bcc_emails)
                
                self.api.request("PATCH", f"/api/communications/{doc_id}", payload={"status": "sent"})
                log.info("Communication sent successfully")
                status = "sent"
                
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                log.error("Failed to process communication", error=str(e))
                self.api.request("PATCH", f"/api/communications/{doc_id}", payload={"status": "failed"})
            finally:
                duration = time.time() - start_time
                email_processing_duration_seconds.record(duration)
                emails_processed_total.add(1, {"status": status, "recipient_count": recipient_count})
                structlog.contextvars.clear_contextvars()

if __name__ == "__main__":
    worker = CommunicationsWorker()
    worker.start()