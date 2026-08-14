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

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let sock = null; // the Baileys socket
let qrCode = null; // latest QR string (null once paired)
let connected = false;
let dailySent = 0;
let dailyDate = new Date().toDateString();
let lastSendTime = 0;
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
      // Baileys expects the JID format: <number>@s.whatsapp.net
      const jid = phone.replace(/[^0-9]/g, "") + "@s.whatsapp.net";
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
