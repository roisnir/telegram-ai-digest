// Minimal Baileys sender. Reads the message text on stdin, posts it to the
// WhatsApp channel in WHATSAPP_CHANNEL_JID. Runs alongside the Telegram send,
// never instead of it.
//
//   node send.js --jid <channel invite link>   resolve a channel's JID (one-off setup)
//   echo "text" | node send.js                 send to WHATSAPP_CHANNEL_JID
//
// ponytail: auth state is a directory of JSON files (useMultiFileAuthState).
// Fine for one sender on one host; move to a DB-backed store only if this ever
// runs from more than one place.
import { readFileSync } from 'node:fs';
import makeWASocket, {
  useMultiFileAuthState,
  DisconnectReason,
  makeCacheableSignalKeyStore,
} from '@whiskeysockets/baileys';
import qrcode from 'qrcode-terminal';

const AUTH_DIR = process.env.WHATSAPP_AUTH_DIR || new URL('./auth', import.meta.url).pathname;
const JID_ARG = process.argv[process.argv.indexOf('--jid') + 1];
const RESOLVE = process.argv.includes('--jid');

const log = (...a) => console.error(...a);

let pendingSave = Promise.resolve();

async function connect(attempt = 0) {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const sock = makeWASocket({
    auth: { creds: state.creds, keys: makeCacheableSignalKeyStore(state.keys) },
    printQRInTerminal: false,
    syncFullHistory: false,
    browser: ['telegram-ai-digest', 'Chrome', '1.0.0'],
  });
  // saveCreds truncates-then-writes; exiting mid-write leaves a 0-byte
  // creds.json and the next run is logged out. Serialise the saves and await
  // the last one before the process exits.
  sock.ev.on('creds.update', () => {
    pendingSave = pendingSave.then(saveCreds).catch((e) => log(`creds save failed: ${e.message}`));
  });

  return new Promise((resolve, reject) => {
    sock.ev.on('connection.update', (u) => {
      const { connection, lastDisconnect, qr } = u;
      if (qr) {
        log('\nScan this QR in WhatsApp → Settings → Linked devices:\n');
        qrcode.generate(qr, { small: true });
      }
      if (connection === 'open') resolve(sock);
      if (connection === 'close') {
        const code = lastDisconnect?.error?.output?.statusCode;
        if (code === DisconnectReason.loggedOut) {
          reject(new Error(`logged out — delete ${AUTH_DIR} and re-pair`));
        } else if (attempt < 3) {
          // 515 (restart required) right after pairing is normal; so is the odd
          // mid-session drop. Creds are already saved, so just dial back in.
          log(`reconnecting (close ${code ?? 'unknown'}, attempt ${attempt + 1})`);
          resolve(connect(attempt + 1));
        } else {
          reject(new Error(`connection closed (${code ?? 'unknown'}): ${lastDisconnect?.error?.message ?? ''}`));
        }
      }
    });
  });
}

const sock = await connect();

if (RESOLVE) {
  // Channels do not show up in the history sync, so the invite link is the way
  // in: https://whatsapp.com/channel/<code> (Channel → info → Share link).
  if (!JID_ARG) throw new Error('usage: node send.js --jid <channel invite link or code>');
  const code = JID_ARG.trim().replace(/^.*\/channel\//, '').split(/[?#]/)[0];
  const meta = await sock.newsletterMetadata('invite', code);
  if (!meta?.id) throw new Error(`no channel found for invite code ${code}`);
  log(`${meta.name ?? ''} — set WHATSAPP_CHANNEL_JID to:`);
  console.log(meta.id);
} else {
  const jid = process.env.WHATSAPP_CHANNEL_JID;
  if (!jid) throw new Error('WHATSAPP_CHANNEL_JID not set');
  const text = readFileSync(0, 'utf8').trim();
  if (!text) throw new Error('empty message on stdin');
  const sent = await sock.sendMessage(jid, { text });
  log(`sent ${sent?.key?.id} to ${jid}`);
}

await pendingSave;
await sock.ws.close();
process.exit(0);
