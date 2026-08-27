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
app.use(express.json());

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
  const { phone, message } = req.body || {};

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
      const result = await sock.sendMessage(jid, { text: message });

      lastSendTime = Date.now();
      dailySent += 1;

      const key = result?.key;
      res.json({
        status: "sent",
        message_id: key?.id || "",
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
// The only reliable answer for countries whose numbering plan does not
// separate mobile from landline, which is most of them outside Asia. Paced
// like /send and capped per request: a burst of lookups for numbers that
// have never been messaged is exactly the pattern WhatsApp rate-limits.
app.post("/check", async (req, res) => {
  const { phones } = req.body || {};

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

  // Share the send queue so a check can never overlap a send.
  sendQueue = sendQueue.then(async () => {
    const results = {};
    for (const phone of phones) {
      const jid = toJid(phone);
      if (!jid) {
        results[phone] = { registered: false, error: "not international format" };
        continue;
      }
      const wait = Math.max(0, CHECK_INTERVAL_MS - (Date.now() - lastCheckTime));
      if (wait > 0) await new Promise((r) => setTimeout(r, wait));
      try {
        const [hit] = await sock.onWhatsApp(jid);
        results[phone] = { registered: Boolean(hit && hit.exists) };
      } catch (err) {
        logger.error({ err, phone }, "Check failed");
        // Unknown, not "no" -- writing "no" here would permanently retire a
        // lead because the network hiccuped once.
        results[phone] = { registered: null, error: err.message || "lookup failed" };
      }
      lastCheckTime = Date.now();
    }
    res.json({ results, checked_at: new Date().toISOString() });
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
