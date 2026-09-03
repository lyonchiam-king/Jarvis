/**
 * WhatsApp relay for Jarvis outreach.
 *
 * A thin Express wrapper around @whiskeysockets/baileys that speaks WhatsApp's
 * multi-device WebSocket protocol. The Python dashboard talks to this via HTTP
 * on localhost — no browser, no Puppeteer, no Cloud API registration.
 *
 * State (auth creds) is persisted to .whatsapp-session/ so a restart does not
 * require re-pairing the phone.
 */

const express = require("express");
const {
  makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
} = require("@whiskeysockets/baileys");
const QRCode = require("qrcode");
const pino = require("pino");

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const PORT = parseInt(process.env.RELAY_PORT || "4099", 10);
const AUTH_DIR = process.env.AUTH_DIR || ".whatsapp-session";
const MAX_PER_DAY = parseInt(process.env.MAX_PER_DAY || "30", 10);
const MIN_INTERVAL_MS = parseInt(process.env.MIN_INTERVAL_SECONDS || "15", 10) * 1000;
// Registration lookups are cheaper than sends and carry no message, so they
// pace faster -- but they still pace. Checking is cached upstream, so this
// runs once per lead in its lifetime, not once per run.
const CHECK_INTERVAL_MS =
  parseInt(process.env.CHECK_INTERVAL_SECONDS || "3", 10) * 1000;
const CHECK_MAX_PER_REQUEST = parseInt(process.env.CHECK_MAX_PER_REQUEST || "50", 10);
const ACK_TIMEOUT_MS = parseInt(process.env.ACK_TIMEOUT_SECONDS || "8", 10) * 1000;

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let sock = null; // the Baileys socket
let qrCode = null; // latest QR string (null once paired)
let connected = false;
let dailySent = 0;
let dailyDate = new Date().toDateString();
let lastSendTime = 0;
let lastCheckTime = 0;
// message id -> WhatsApp status code (1 = sent to server, 2 = delivered).
const acks = new Map();

// Wait briefly for the server to acknowledge a message. Resolves to the
// status code, or null if nothing came back in time -- which is reported as
// "pending", never as "sent".
async function waitForAck(id, timeoutMs = ACK_TIMEOUT_MS) {
  if (!id) return null;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (acks.has(id)) return acks.get(id);
    await new Promise((r) => setTimeout(r, 200));
  }
  return acks.has(id) ? acks.get(id) : null;
}
let sendQueue = Promise.resolve(); // serialise sends so we never firehose

// ---------------------------------------------------------------------------
// Logger — pino but quiet (Baileys is chatty at info level)
// ---------------------------------------------------------------------------

const logger = pino({ level: "warn" });

// ---------------------------------------------------------------------------
// Daily counter
// ---------------------------------------------------------------------------

function resetDailyIfNeeded() {
  const today = new Date().toDateString();
  if (today !== dailyDate) {
    dailyDate = today;
    dailySent = 0;
  }
}

// ---------------------------------------------------------------------------
// Connect to WhatsApp
// ---------------------------------------------------------------------------

async function connect() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();

  sock = makeWASocket({
    version,
    auth: state,
    printQRInTerminal: false,
    logger,
    browser: ["Jarvis Outreach", "Desktop", "1.0.0"],
  });

  sock.ev.on("creds.update", saveCreds);

  // WhatsApp acknowledges a message separately from sendMessage() resolving.
  // Without this the relay reported "sent" the moment the call returned,
  // which is true of a message the socket merely queued and never
  // transmitted -- so a send could look successful and arrive nowhere, with
  // nothing in the sender's own chat list either.
  sock.ev.on("messages.update", (updates) => {
    for (const u of updates || []) {
      const id = u?.key?.id;
      const status = u?.update?.status;
      if (id && typeof status === "number") {
        acks.set(id, status);
        // Bounded: this is a short-lived correlation table, not a log.
        if (acks.size > 500) acks.delete(acks.keys().next().value);
      }
    }
  });

  sock.ev.on("connection.update", (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      // Convert the raw QR string to a base64 PNG for the dashboard.
      QRCode.toDataURL(qr, { width: 300, margin: 2 })
        .then((dataUrl) => {
          qrCode = dataUrl;
        })
        .catch((err) => {
          logger.error({ err }, "QR generation failed");
        });
    }

    if (connection === "open") {
      connected = true;
      qrCode = null;
      console.log("[whatsapp-relay] Connected to WhatsApp");
    }

    if (connection === "close") {
      connected = false;
      const statusCode = lastDisconnect?.error?.output?.statusCode;
      const shouldReconnect = statusCode !== DisconnectReason.loggedOut;

      console.log(
        `[whatsapp-relay] Disconnected (code: ${statusCode}). Reconnect: ${shouldReconnect}`
      );

      if (shouldReconnect) {
        setTimeout(connect, 3000);
      } else {
        qrCode = null;
        console.log("[whatsapp-relay] Logged out — waiting for new QR scan");
        // Reconnect anyway so a new QR is generated.
        setTimeout(connect, 5000);
      }
    }
  });
}

// ---------------------------------------------------------------------------
// HTTP API
// ---------------------------------------------------------------------------

const app = express();
// The default body limit is 100kb, which a text message never approached
// and a screenshot always exceeds: a 900x1200 PNG is a few hundred KB and
// base64 adds a third on top. Express rejected those requests with 413
// before the send route ran at all, so sending worked perfectly until the
// day messages started carrying a picture.
app.use(express.json({ limit: process.env.MAX_BODY || "25mb" }));

// A body that is still too large must not come back as Express's HTML
// error page, which the caller cannot parse and reports as a transport
// failure -- saying nothing about the size being the problem.
app.use((err, _req, res, next) => {
  if (err && (err.type === "entity.too.large" || err.status === 413)) {
    return res.status(413).json({
      status: "failed",
      error:
        "The message and its picture are larger than the relay accepts. "
        + "Send without the screenshot, or raise MAX_BODY.",
    });
  }
  return next(err);
});

// GET /status — connection state + daily counters
app.get("/status", (_req, res) => {
  resetDailyIfNeeded();
  res.json({
    connected,
    paired: connected, // Baileys: open socket = paired
    daily_sent: dailySent,
    daily_cap: MAX_PER_DAY,
    port: PORT,
  });
});

// GET /qr — base64 PNG of the pairing QR, or null if already paired
app.get("/qr", (_req, res) => {
  res.json({ qr: qrCode });
});

// POST /send — { phone, message }
app.post("/send", async (req, res) => {
  // `image` is optional base64 PNG/JPEG bytes. With one, the message goes
  // as a picture with the text as its caption -- a screenshot of the site
  // says more in a phone notification than a link nobody taps.
  const { phone, message, image } = req.body || {};

  if (!phone || !message) {
    return res.status(400).json({
      status: "failed",
      error: "phone and message are required",
    });
  }

  if (!connected || !sock) {
    return res.status(503).json({
      status: "failed",
      error: "WhatsApp is not connected",
    });
  }

  resetDailyIfNeeded();

  if (dailySent >= MAX_PER_DAY) {
    return res.status(429).json({
      status: "failed",
      error: `Daily cap of ${MAX_PER_DAY} messages reached`,
    });
  }

  // Serialise sends so we never fire two at once, and enforce the gap.
  sendQueue = sendQueue.then(async () => {
    const now = Date.now();
    const wait = Math.max(0, MIN_INTERVAL_MS - (now - lastSendTime));
    if (wait > 0) {
      await new Promise((r) => setTimeout(r, wait));
    }

    try {
      // Baileys expects the JID in international format:
      // <country><number>@s.whatsapp.net. The caller normalises to E.164 --
      // outreach/phone.py knows each lead's country and this process does
      // not -- so the number arrives ready and is used as given.
      //
      // This used to force a "60" onto anything that lacked one, which was
      // right in Penang and dangerous everywhere else: a US number
      // 15045550123 became 6015045550123, a real Malaysian line belonging
      // to a stranger. Never re-guess a country code here.
      const jid = toJid(phone);
      if (!jid) {
        lastSendTime = Date.now();
        return res.status(400).json({
          status: "failed",
          error: "phone must be in international format, digits only",
        });
      }
      // Confirm the recipient exists before sending. WhatsApp accepts a
      // message for any well-formed JID and reports nothing wrong, so
      // sending blind to a number that is not registered looks exactly like
      // success and reaches nobody.
      const digits = jid.split("@")[0];
      let registered = null;
      try {
        const found = await sock.onWhatsApp(digits);
        registered = (found || []).some((e) => {
          const back = String(e?.jid || "").replace(/[^0-9]/g, "");
          return (back === digits || back.endsWith(digits)) && e?.exists !== false;
        });
      } catch (err) {
        // A failed check is not proof of absence, so the send still goes.
        logger.warn({ err, phone }, "Pre-send check failed; sending anyway");
      }
      // Deliberately does not block. The queue only offers leads whose
      // cached verdict is already "yes", so a live "no" here contradicts an
      // earlier answer from the same API and is far more likely a hiccup
      // than the truth -- and refusing on it stops the send outright. It is
      // reported alongside the result instead.
      const unregistered = registered === false;

      let payload = { text: message };
      if (image) {
        try {
          const buffer = Buffer.from(image, "base64");
          if (!buffer.length) throw new Error("empty image");
          payload = { image: buffer, caption: message };
        } catch (err) {
          // A bad attachment must not cost the message. Sending the text
          // alone is worth far more than failing the whole send.
          logger.warn({ err, phone }, "Attachment unusable; sending text only");
        }
      }
      const result = await sock.sendMessage(jid, payload);

      lastSendTime = Date.now();
      dailySent += 1;

      const key = result?.key;
      // "sent" now means WhatsApp acknowledged it, not merely that the call
      // returned. Without an ack the honest answer is "pending": the message
      // may be sitting in a socket that never transmitted it.
      const ack = await waitForAck(key?.id);
      // An ack is evidence, not a gate. WhatsApp does not always emit
      // messages.update promptly -- a first message to a new contact often
      // confirms late -- and treating silence as failure turned working
      // sends into errors and stopped the queue dead. Report what is known:
      // "sent" when confirmed, "unconfirmed" when the message left but
      // nothing came back yet. Neither is a failure.
      res.json({
        status: ack === null ? "unconfirmed" : "sent",
        message_id: key?.id || "",
        dialled: digits,
        ack: ack,
        warning: unregistered
          ? `WhatsApp did not recognise ${digits} just now, though it was `
            + "confirmed earlier. The message was still sent."
          : undefined,
        note:
          ack === null
            ? "WhatsApp has not confirmed delivery yet. The message was "
              + "accepted; check the chat on your phone if it matters."
            : undefined,
        timestamp: new Date().toISOString(),
      });
    } catch (err) {
      logger.error({ err, phone }, "Send failed");
      res.json({
        status: "failed",
        error: err.message || "Unknown error",
        timestamp: new Date().toISOString(),
      });
    }
  });
});

// Digits of an E.164 number -> a WhatsApp JID. No country is inferred: a
// number that does not already carry its country code is refused rather than
// sent somewhere plausible-looking.
function toJid(phone) {
  const digits = String(phone || "").replace(/[^0-9]/g, "");
  // Shortest real E.164 subscriber numbers are 8 digits including the code.
  if (digits.length < 8 || digits.length > 15) return "";
  if (digits.startsWith("0")) return "";
  return digits + "@s.whatsapp.net";
}

// POST /check — is this number registered on WhatsApp?
//
// The only reliable answer for a country whose numbering plan does not
// separate mobile from landline -- and for landlines everywhere, since
// WhatsApp Business verifies fixed lines by voice call. Paced like /send and
// capped per request: a burst of lookups for numbers never messaged is
// exactly the pattern WhatsApp rate-limits.
//
// onWhatsApp() takes a PHONE NUMBER, not a JID. It builds the query by
// prefixing "+" to whatever it is handed, so passing 4479...@s.whatsapp.net
// asks about "+4479...@s.whatsapp.net", which matches nothing -- every
// number came back "not on WhatsApp", in every country. Pass bare digits.
app.post("/check", async (req, res) => {
  const { phones, debug } = req.body || {};

  if (!Array.isArray(phones) || phones.length === 0) {
    return res.status(400).json({ error: "phones must be a non-empty array" });
  }
  if (phones.length > CHECK_MAX_PER_REQUEST) {
    return res.status(400).json({
      error: `at most ${CHECK_MAX_PER_REQUEST} numbers per request`,
    });
  }
  if (!connected || !sock) {
    return res.status(503).json({ error: "WhatsApp is not connected" });
  }

  // Share the send queue so a check never overlaps a send. Everything inside
  // is wrapped: an escaping throw would both hang this request and poison
  // sendQueue for the life of the process, silently killing every later send.
  sendQueue = sendQueue.then(async () => {
    const results = {};
    try {
      for (const phone of phones) {
        const digits = String(phone || "").replace(/[^0-9]/g, "");
        if (digits.length < 8 || digits.length > 15 || digits.startsWith("0")) {
          results[phone] = { registered: false, error: "not international format" };
          continue;
        }

        const wait = Math.max(0, CHECK_INTERVAL_MS - (Date.now() - lastCheckTime));
        if (wait > 0) await new Promise((r) => setTimeout(r, wait));

        try {
          const found = await sock.onWhatsApp(digits);
          // Match on the digits that come back rather than on position:
          // Baileys omits numbers that do not exist, so index 0 of a short
          // reply can belong to a different number than the one asked about.
          const hit = (found || []).find((entry) => {
            const back = String(entry?.jid || "").replace(/[^0-9]/g, "");
            return back === digits || back.endsWith(digits) || digits.endsWith(back);
          });
          results[phone] = { registered: Boolean(hit && hit.exists !== false) };
          if (debug) {
            results[phone].raw = found;
            results[phone].asked = digits;
          }
        } catch (err) {
          logger.error({ err, phone }, "Check failed");
          // Unknown, not "no" -- writing "no" would permanently retire a lead
          // because the network hiccuped once.
          results[phone] = { registered: null, error: err.message || "lookup failed" };
        }
        lastCheckTime = Date.now();
      }
      res.json({ results, checked_at: new Date().toISOString() });
    } catch (err) {
      logger.error({ err }, "Check batch failed");
      if (!res.headersSent) {
        res.status(500).json({ error: err.message || "check failed" });
      }
    }
  });
});

// POST /logout — destroy the session (forces re-pair)
app.post("/logout", async (_req, res) => {
  if (sock) {
    try {
      await sock.logout();
    } catch {
      // Best-effort — the socket may already be dead.
    }
  }
  connected = false;
  qrCode = null;
  res.json({ ok: true });
});

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

connect().then(() => {
  app.listen(PORT, "127.0.0.1", () => {
    console.log(`[whatsapp-relay] Listening on http://127.0.0.1:${PORT}`);
  });
});
