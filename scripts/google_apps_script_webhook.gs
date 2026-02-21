/**
 * Google Apps Script webhook for Dalsjöfors Hyrservice.
 *
 * Supports:
 * - Receipt emails (booking.confirmed)
 * - Issue report emails (type=issue_report) with clear Swedish formatting
 *
 * Script properties:
 * - WEBHOOK_SECRET or NOTIFY_WEBHOOK_SECRET (optional, but recommended)
 */

function doPost(e) {
  var payload = _parsePayload(e);
  if (!payload.ok) {
    return _jsonResponse({ ok: false, error: "invalid_json" });
  }

  var body = payload.body;
  var expectedSecret = _trim(
    PropertiesService.getScriptProperties().getProperty("WEBHOOK_SECRET") ||
      PropertiesService.getScriptProperties().getProperty("NOTIFY_WEBHOOK_SECRET") ||
      ""
  );
  var providedSecret = _trim(body.secret || "");
  if (expectedSecret && providedSecret !== expectedSecret) {
    return _jsonResponse({ ok: false, error: "unauthorized" });
  }

  if (body.type === "issue_report") {
    return _handleIssueReport(body);
  }

  if (body.event === "booking.confirmed" && body.receiptRequested === true) {
    return _handleReceipt(body);
  }

  return _jsonResponse({ ok: false, error: "unsupported_payload" });
}

function _handleIssueReport(body) {
  var to = _trim(body.to || "");
  if (!to) {
    return _jsonResponse({ ok: false, error: "missing_to" });
  }

  var view = _buildIssueView(body);
  var subject = "Skaderapport: " + view.slap + " — " + view.bookingRefOrFallback;
  var mailBody = _buildIssueBody(view);
  var options = {};
  var attachments = _decodeAttachments(body.attachments);
  if (attachments.length > 0) {
    options.attachments = attachments;
  }

  GmailApp.sendEmail(to, subject, mailBody, options);
  return _jsonResponse({ ok: true, type: "issue_report", sentTo: to });
}

function _handleReceipt(body) {
  var customerEmail = _trim(body.customerEmail || "");
  if (!customerEmail) {
    return _jsonResponse({ ok: false, error: "missing_customer_email" });
  }

  var companyName = _trim(body.companyName || "Dalsjöfors Hyrservice AB");
  var bookingReference = _trim(body.bookingReference || "");
  var trailerType = _trim(body.trailerType || "");
  var startDt = _trim(body.startDt || "");
  var endDt = _trim(body.endDt || "");
  var price = body.price;

  var subject = "Kvitto för bokning " + (bookingReference || "(utan referens)");
  var lines = [
    "Hej!",
    "",
    "Tack för din bokning hos " + companyName + ".",
    "",
    "Bokningsreferens: " + bookingReference,
    "Släp: " + trailerType,
    "Start: " + startDt,
    "Slut: " + endDt,
    "Belopp: " + price + " kr",
    "",
    "Hälsningar,",
    companyName,
  ];

  GmailApp.sendEmail(customerEmail, subject, lines.join("\n"));
  return _jsonResponse({ ok: true, event: "booking.confirmed", sentTo: customerEmail });
}

function _buildIssueView(body) {
  var friendly = body.friendlyFields || {};
  var fields = body.fields || {};

  var slap = _first(
    _trim(friendly["Släp"]),
    _trim(fields.trailer_label),
    _trim(fields.trailer_type),
    "Okänt släp"
  );
  var bookingRef = _first(
    _trim(friendly["Bokningsreferens"]),
    _trim(fields.booking_reference),
    _trim(body.bookingRef),
    ""
  );
  var reportType = _first(
    _trim(friendly["Typ av rapport"]),
    _trim(fields.report_type_label),
    _mapReportType(_trim(fields.report_type)),
    "Okänd rapporttyp"
  );
  var detectedAt = _first(
    _trim(friendly["Upptäckt datum/tid"]),
    _trim(fields.detected_at),
    "Ej angivet"
  );
  var namn = _first(_trim(friendly["Namn"]), _trim(fields.name), "Ej angivet");
  var telefon = _first(_trim(friendly["Telefon"]), _trim(fields.phone), "Ej angivet");
  var epost = _first(_trim(friendly["E-post"]), _trim(fields.email), "Ej angivet");
  var beskrivning = _first(_trim(friendly["Beskrivning"]), _trim(body.message), _trim(fields.message), "Ej angivet");

  var attachmentNames = [];
  if (Array.isArray(body.attachmentNames)) {
    for (var i = 0; i < body.attachmentNames.length; i++) {
      var name = _trim(body.attachmentNames[i]);
      if (name) attachmentNames.push(name);
    }
  }
  if (attachmentNames.length === 0 && Array.isArray(body.attachments)) {
    for (var j = 0; j < body.attachments.length; j++) {
      var attachment = body.attachments[j] || {};
      var filename = _trim(attachment.filename || "");
      if (filename) attachmentNames.push(filename);
    }
  }
  var attachmentCount = Number(body.attachmentCount);
  if (!isFinite(attachmentCount) || attachmentCount < 0) {
    attachmentCount = attachmentNames.length;
  }

  return {
    slap: slap,
    bookingRef: bookingRef,
    bookingRefOrFallback: bookingRef || "Ingen bokning",
    reportType: reportType,
    detectedAt: detectedAt,
    namn: namn,
    telefon: telefon,
    epost: epost,
    beskrivning: beskrivning,
    attachmentCount: attachmentCount,
    attachmentNames: attachmentNames,
    reportId: _trim(body.reportId || ""),
    submittedAt: _trim(body.submittedAt || ""),
  };
}

function _buildIssueBody(view) {
  var fileText = view.attachmentNames.length > 0
    ? view.attachmentNames.join(", ")
    : "Inga bifogade bilder";
  return [
    "NY SKADERAPPORT",
    "----------------",
    "Släp: " + view.slap,
    "Bokningsreferens: " + view.bookingRefOrFallback,
    "Typ av rapport: " + view.reportType,
    "Upptäckt datum/tid: " + view.detectedAt,
    "",
    "KONTAKT",
    "-------",
    "Namn: " + view.namn,
    "Telefon: " + view.telefon,
    "E-post: " + view.epost,
    "",
    "BESKRIVNING",
    "-----------",
    view.beskrivning,
    "",
    "BILDER",
    "------",
    "Antal: " + String(view.attachmentCount),
    "Filer: " + fileText,
    "",
    "TEKNISKT",
    "--------",
    "Rapport-ID: " + (view.reportId || "saknas"),
    "Inskickad: " + (view.submittedAt || "saknas"),
  ].join("\n");
}

function _decodeAttachments(attachmentsPayload) {
  if (!Array.isArray(attachmentsPayload)) return [];
  var result = [];
  for (var i = 0; i < attachmentsPayload.length; i++) {
    var item = attachmentsPayload[i] || {};
    var base64Data = _trim(item.dataBase64 || "");
    if (!base64Data) continue;
    try {
      var bytes = Utilities.base64Decode(base64Data);
      var filename = _trim(item.filename || ("bild-" + (i + 1) + ".jpg"));
      var contentType = _trim(item.contentType || "application/octet-stream");
      result.push(Utilities.newBlob(bytes, contentType, filename));
    } catch (err) {
      // Ignore malformed attachments; email should still be delivered.
    }
  }
  return result;
}

function _mapReportType(raw) {
  if (raw === "BEFORE_RENTAL") return "Upptäckt innan hyra";
  if (raw === "DURING_RENTAL") return "Skada under hyra";
  if (raw === "OTHER") return "Annat";
  return raw || "";
}

function _parsePayload(e) {
  try {
    var contents = e && e.postData && e.postData.contents ? e.postData.contents : "{}";
    return { ok: true, body: JSON.parse(contents) };
  } catch (err) {
    return { ok: false, body: null };
  }
}

function _jsonResponse(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function _trim(value) {
  return String(value == null ? "" : value).trim();
}

function _first() {
  for (var i = 0; i < arguments.length; i++) {
    var value = arguments[i];
    if (_trim(value)) return _trim(value);
  }
  return "";
}
