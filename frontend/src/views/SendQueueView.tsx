import React, { useState, useEffect } from 'react';
import {
  Send,
  Smartphone,
  CheckCircle2,
  AlertCircle,
  HelpCircle,
  Clock,
  ExternalLink,
  Edit3,
  Sparkles,
  ShieldAlert,
} from 'lucide-react';
import { SendItem, SendStatus, WhatsAppStatus } from '../types';
import { api } from '../api/client';

interface SendQueueViewProps {
  whatsappStatus: WhatsAppStatus | null;
  onRefresh: () => void;
}

export const SendQueueView: React.FC<SendQueueViewProps> = ({
  whatsappStatus,
  onRefresh,
}) => {
  const [queue, setQueue] = useState<SendItem[]>([]);
  const [excluded, setExcluded] = useState<{ name: string; phone: string; reason: string }[]>([]);
  const [loading, setLoading] = useState(true);
  const [messagesMap, setMessagesMap] = useState<Record<string, string>>({});
  const [sendingMap, setSendingMap] = useState<Record<string, boolean>>({});
  const [sendingAll, setSendingAll] = useState(false);
  const [showConfirmModal, setShowConfirmModal] = useState(false);
  // What the last action actually did, per lead, and any error worth
  // showing. A send that fails silently reads as a send that worked, and
  // the only correction for that is to message the business twice.
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [sendError, setSendError] = useState<string | null>(null);
  // Which cards have had their click-to-chat link followed. WhatsApp opens
  // in another app and tells this page nothing, so the card has to ask
  // afterwards -- leaving it alone would keep offering the same business.
  const [handedMap, setHandedMap] = useState<Record<string, boolean>>({});

  // AI Rewrite drawer/inline state
  const [rewriteLeadId, setRewriteLeadId] = useState<string | null>(null);
  const [rewriteInstruction, setRewriteInstruction] = useState('');
  const [rewriting, setRewriting] = useState(false);

  const loadQueue = async () => {
    setLoading(true);
    const res = await api.getSendQueue();
    if (res.data) {
      setQueue(res.data.queue);
      setExcluded(res.data.excluded);
      const initialMsgs: Record<string, string> = {};
      res.data.queue.forEach((item) => {
        initialMsgs[item.lead_id] = item.whatsapp_message;
      });
      setMessagesMap(initialMsgs);
    }
    setLoading(false);
  };

  useEffect(() => {
    loadQueue();
  }, []);

  /**
   * Send one message.
   *
   * `e164_phone` is passed through untouched. It was worked out on the
   * Python side, where the lead's own country is known; the browser must
   * never derive it, and the server must not derive it twice. A number
   * normalised a second time grows a second country code -- a UK lead once
   * went out as 60447450225230, which WhatsApp accepts, reports as sent,
   * and delivers to nobody.
   */
  const sendOne = async (item: SendItem): Promise<boolean> => {
    setSendingMap((prev) => ({ ...prev, [item.lead_id]: true }));
    const res = await api.sendWhatsApp(
      item.lead_id,
      messagesMap[item.lead_id],
      item.e164_phone
    );
    setSendingMap((prev) => ({ ...prev, [item.lead_id]: false }));

    if (res.error) {
      setSendError(res.error);
      setNotes((prev) => ({ ...prev, [item.lead_id]: res.error! }));
      return false;
    }
    // `unconfirmed` means the message left and WhatsApp had not acknowledged
    // it inside the relay's window. That is missing evidence, not evidence
    // of nothing -- the send stands, and the page says so rather than
    // inviting a second attempt at the same business.
    const status = res.data?.status || 'sent';
    setNotes((prev) => ({
      ...prev,
      [item.lead_id]:
        status === 'unconfirmed'
          ? 'Sent. WhatsApp has not confirmed delivery yet.'
          : `Sent (${status}).`,
    }));
    return true;
  };

  const handleSendSingle = async (lead_id: string) => {
    const item = queue.find((q) => q.lead_id === lead_id);
    if (!item) return;
    setSendError(null);
    await sendOne(item);
    await loadQueue();
    onRefresh();
  };

  const handleSendAll = async () => {
    setShowConfirmModal(false);
    setSendingAll(true);
    setSendError(null);
    let failed = 0;
    for (const item of queue) {
      // Stop at the first failure rather than working through the rest.
      // The usual reason a send fails is the relay dropping, and carrying
      // on then produces a column of identical errors and no messages.
      if (!(await sendOne(item))) {
        failed += 1;
        break;
      }
    }
    setSendingAll(false);
    if (failed) setSendError((prev) => prev && `${prev} -- stopped, nothing further was sent.`);
    await loadQueue();
    onRefresh();
  };

  /**
   * The click-to-chat link for one card, carrying whatever is in the box.
   *
   * Only the text is replaced. The part before `?` is `https://wa.me/<digits>`
   * as Python built it, where the lead's own country was known -- the same
   * rule the relay send follows, and for the same reason: a number the
   * browser assembles grows a second country code and reaches a stranger.
   */
  const chatLink = (item: SendItem): string => {
    if (!item.wa_link) return '';
    const text = messagesMap[item.lead_id] ?? item.whatsapp_message;
    return `${item.wa_link.split('?')[0]}?text=${encodeURIComponent(text || '')}`;
  };

  const handleMarkSent = async (lead_id: string) => {
    setHandedMap((prev) => ({ ...prev, [lead_id]: false }));
    setSendingMap((prev) => ({ ...prev, [lead_id]: true }));
    const res = await api.markWhatsAppSent(lead_id);
    setSendingMap((prev) => ({ ...prev, [lead_id]: false }));
    if (res.error) {
      setSendError(res.error);
      return;
    }
    // Said plainly: this page never saw the message leave, so what it
    // recorded is your word for it.
    setNotes((prev) => ({
      ...prev,
      [lead_id]: 'Recorded from your phone. Send again puts it back if it never arrived.',
    }));
    await loadQueue();
    onRefresh();
  };

  const handleRewrite = async (lead_id: string) => {
    if (!rewriteInstruction) return;
    setRewriting(true);
    const res = await api.rewriteOutreach(lead_id, rewriteInstruction);
    if (res.error) {
      setRewriting(false);
      setSendError(res.error);
      return;
    }
    // The rewrite is written to the Sheet rather than returned, so the new
    // text has to be read back. Reloading the queue is also what proves the
    // save landed -- a message shown in the box but never stored is exactly
    // the sort of thing that gets sent in its old form.
    await loadQueue();
    setRewriting(false);
    setRewriteLeadId(null);
    setRewriteInstruction('');
  };

  if (loading) {
    return (
      <div id="send-loading" className="p-8 text-center text-slate-500 animate-pulse">
        Loading outreach send queue...
      </div>
    );
  }

  const pendingQueue = queue.filter((item) => !item.status || item.status === 'failed');

  return (
    <div id="send-queue-view" className="space-y-6">
      {/* Top Header & Send All Control */}
      <div className="p-5 rounded-xl border bg-white dark:bg-slate-900 border-slate-200 dark:border-slate-800 shadow-xs flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h1 className="text-lg font-bold text-slate-900 dark:text-slate-100 flex items-center gap-2">
            <Send className="w-5 h-5 text-emerald-600" /> WhatsApp Send Queue ({queue.length} Built Sites)
          </h1>
          <p className="text-xs text-slate-500 dark:text-slate-400 mt-0.5">
            Review custom WhatsApp messages, edit inline, and transmit via Local Relay socket.
          </p>
        </div>

        {/* Only where a relay can act on it. Sending the batch by hand is
            not a thing that exists -- each one opens WhatsApp and waits for
            a person -- so a batch button with no relay is a button that
            cannot work, which is worse than no button at all. */}
        {whatsappStatus?.connected && (
          <button
            id="send-all-btn"
            onClick={() => setShowConfirmModal(true)}
            disabled={sendingAll || pendingQueue.length === 0}
            className="inline-flex items-center gap-2 px-5 py-2.5 bg-emerald-600 hover:bg-emerald-500 text-white font-bold text-xs rounded-lg shadow-xs transition disabled:opacity-50"
          >
            <Send className="w-4 h-4" />
            {sendingAll ? 'Transmitting Batch...' : `Send All (${pendingQueue.length} Pending)`}
          </button>
        )}
      </div>

      {sendError && (
        <div
          id="send-error-banner"
          className="p-4 rounded-xl bg-rose-50 dark:bg-rose-950/40 border border-rose-300 dark:border-rose-800 text-rose-900 dark:text-rose-200 text-xs flex items-start gap-2"
        >
          <AlertCircle className="w-4 h-4 text-rose-600 shrink-0 mt-0.5" />
          <span>
            <strong>The last send did not go through.</strong> {sendError}
          </span>
        </div>
      )}

      {/* Relay Disconnected Warning Banner if missing */}
      {!whatsappStatus?.connected && (
        <div className="p-4 rounded-xl bg-amber-50 dark:bg-amber-950/40 border border-amber-300 dark:border-amber-800 text-amber-900 dark:text-amber-200 text-xs flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Smartphone className="w-4 h-4 text-amber-600 shrink-0" />
            <span>
              <strong>Relay not connected</strong>
              {whatsappStatus?.relayUrl ? ` on ${whatsappStatus.relayUrl}` : ''}
              {whatsappStatus?.error ? ` -- ${whatsappStatus.error}` : ''}. Nothing
              can be sent from here, and nothing is queued or copied anywhere --
              the messages below stay exactly as they are. Use{' '}
              <strong>Send on my phone</strong> on each card: it opens WhatsApp
              with the message already written and you press send there.
            </span>
          </div>
        </div>
      )}

      {/* Main Queue Cards */}
      <div className="space-y-4">
        {queue.map((item) => {
          const isSending = sendingMap[item.lead_id] || false;
          const currentMsg = messagesMap[item.lead_id] || item.whatsapp_message;

          return (
            <div
              key={item.lead_id}
              id={`send-card-${item.lead_id}`}
              className="p-5 rounded-xl border bg-white dark:bg-slate-900 border-slate-200 dark:border-slate-800 shadow-xs grid grid-cols-1 lg:grid-cols-12 gap-6"
            >
              {/* Column 1: Site Thumbnail Preview (4 cols) */}
              <div className="lg:col-span-4 space-y-2">
                <div className="flex items-center justify-between text-xs font-semibold">
                  <span className="text-slate-800 dark:text-slate-200">{item.business_name}</span>
                  <a
                    href={item.preview_url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-teal-600 dark:text-teal-400 hover:underline flex items-center gap-0.5 text-[11px]"
                  >
                    View Site <ExternalLink className="w-3 h-3" />
                  </a>
                </div>

                <div className="rounded-lg border border-slate-200 dark:border-slate-800 overflow-hidden bg-slate-900 shadow-2xs">
                  <img
                    src={item.screenshot_url || `/api/screenshot/${encodeURIComponent(item.lead_id)}`}
                    alt={`Preview of ${item.business_name}`}
                    // The infographic is 16:9 and IS the pitch, so cropping it
                    // to a tall card hides the thing being sent.
                    className={
                      item.shot_kind === 'visual'
                        ? 'w-full object-contain bg-slate-950'
                        : 'w-full h-44 object-cover'
                    }
                  />
                </div>
                {/* Named, not assumed. The queue has always said which picture
                    it carries; nothing read it, so an infographic was labelled
                    a site screenshot. */}
                <p
                  id={`send-shot-kind-${item.lead_id}`}
                  className="text-[11px] text-slate-500"
                >
                  {item.shot_kind === 'visual'
                    ? 'Infographic attached'
                    : 'Site screenshot attached'}
                </p>

                <div className="text-[11px] font-mono text-slate-500">
                  Recipient Dial: <strong className="text-teal-600 dark:text-teal-400">{item.e164_phone}</strong>
                </div>
              </div>

              {/* Column 2: Message & Send Control (8 cols) */}
              <div className="lg:col-span-8 flex flex-col justify-between space-y-3">
                <div>
                  <div className="flex items-center justify-between mb-1.5">
                    <label className="block text-xs font-bold text-slate-800 dark:text-slate-200 flex items-center gap-1.5">
                      <Edit3 className="w-3.5 h-3.5 text-teal-600" /> Inline Editable WhatsApp Pitch Message
                    </label>

                    <button
                      onClick={() =>
                        setRewriteLeadId(rewriteLeadId === item.lead_id ? null : item.lead_id)
                      }
                      className="text-xs text-teal-600 dark:text-teal-400 hover:underline flex items-center gap-1 font-medium"
                    >
                      <Sparkles className="w-3.5 h-3.5" /> AI Rewrite Instruction
                    </button>
                  </div>

                  {/* AI Rewrite Panel */}
                  {rewriteLeadId === item.lead_id && (
                    <div className="mb-2 p-2.5 rounded-lg bg-teal-50 dark:bg-teal-950/40 border border-teal-200 dark:border-teal-800/60 text-xs space-y-2">
                      <input
                        type="text"
                        placeholder="e.g., 'Make it shorter and mention 50% discount this month'..."
                        value={rewriteInstruction}
                        onChange={(e) => setRewriteInstruction(e.target.value)}
                        className="w-full p-2 bg-white dark:bg-slate-900 border border-teal-300 dark:border-teal-700 rounded text-xs text-slate-900 dark:text-slate-100"
                      />
                      <div className="flex justify-end gap-2">
                        <button
                          onClick={() => setRewriteLeadId(null)}
                          className="px-2.5 py-1 text-slate-500 hover:text-slate-800 text-[11px]"
                        >
                          Cancel
                        </button>
                        <button
                          onClick={() => handleRewrite(item.lead_id)}
                          disabled={rewriting || !rewriteInstruction}
                          className="px-3 py-1 bg-teal-600 hover:bg-teal-500 text-white rounded text-[11px] font-semibold"
                        >
                          {rewriting ? 'Rewriting...' : 'Apply AI Rewrite'}
                        </button>
                      </div>
                    </div>
                  )}

                  <textarea
                    rows={4}
                    value={currentMsg}
                    onChange={(e) =>
                      setMessagesMap((prev) => ({
                        ...prev,
                        [item.lead_id]: e.target.value,
                      }))
                    }
                    className="w-full p-3 bg-slate-50 dark:bg-slate-800/80 border border-slate-200 dark:border-slate-700 rounded-lg text-xs text-slate-900 dark:text-slate-100 font-sans leading-relaxed"
                  />
                </div>

                {/* Bottom Status & Action Bar */}
                <div className="pt-3 border-t border-slate-100 dark:border-slate-800 flex items-center justify-between gap-3">
                  {/* Status Badges */}
                  <div>
                    {item.status === 'delivered' && (
                      <span className="inline-flex items-center gap-1 text-xs font-bold text-emerald-600 dark:text-emerald-400 bg-emerald-500/10 px-2.5 py-1 rounded-full">
                        <CheckCircle2 className="w-3.5 h-3.5" /> Delivered
                      </span>
                    )}
                    {item.status === 'sent' && (
                      <span className="inline-flex items-center gap-1 text-xs font-bold text-teal-600 dark:text-teal-400 bg-teal-500/10 px-2.5 py-1 rounded-full">
                        <CheckCircle2 className="w-3.5 h-3.5" /> Sent
                      </span>
                    )}
                    {item.status === 'read' && (
                      <span className="inline-flex items-center gap-1 text-xs font-bold text-blue-600 dark:text-blue-400 bg-blue-500/10 px-2.5 py-1 rounded-full">
                        <CheckCircle2 className="w-3.5 h-3.5" /> Read
                      </span>
                    )}
                    {/* CRITICAL: Unconfirmed status styled NEUTRALLY, not as error! */}
                    {item.status === 'unconfirmed' && (
                      <span
                        className="inline-flex items-center gap-1 text-xs font-medium text-amber-700 dark:text-amber-300 bg-amber-500/10 border border-amber-500/30 px-2.5 py-1 rounded-full"
                        title="Accepted by WhatsApp socket; delivery receipt pending."
                      >
                        <Clock className="w-3.5 h-3.5" /> Unconfirmed (Pending Receipt)
                      </span>
                    )}
                    {!item.status && (
                      <span className="text-xs text-slate-400 font-medium">Ready to Send</span>
                    )}
                  </div>

                  <div className="flex items-center gap-2 flex-wrap justify-end">
                    {handedMap[item.lead_id] ? (
                      <>
                        <span className="text-xs font-medium text-slate-600 dark:text-slate-300">
                          Did it send?
                        </span>
                        <button
                          id={`mark-sent-btn-${item.lead_id}`}
                          onClick={() => handleMarkSent(item.lead_id)}
                          disabled={isSending}
                          className="inline-flex items-center gap-1.5 px-4 py-2 bg-emerald-600 hover:bg-emerald-500 text-white font-semibold text-xs rounded-lg shadow-xs transition disabled:opacity-50"
                        >
                          <CheckCircle2 className="w-3.5 h-3.5" />
                          {isSending ? 'Recording...' : 'Yes, it sent'}
                        </button>
                        <button
                          onClick={() =>
                            setHandedMap((prev) => ({ ...prev, [item.lead_id]: false }))
                          }
                          className="px-3 py-2 text-xs font-semibold text-slate-600 dark:text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-800 rounded-lg"
                        >
                          Not yet
                        </button>
                      </>
                    ) : (
                      <>
                        {/* An anchor, not a button: it has to open WhatsApp,
                            and a long-press on a phone should be able to copy
                            or open it elsewhere. */}
                        {item.wa_link && (
                          <a
                            id={`hand-off-btn-${item.lead_id}`}
                            href={chatLink(item)}
                            target="_blank"
                            rel="noopener noreferrer"
                            onClick={() =>
                              setHandedMap((prev) => ({ ...prev, [item.lead_id]: true }))
                            }
                            title="Opens WhatsApp with this message already written. You press send there."
                            className="inline-flex items-center gap-1.5 px-4 py-2 bg-emerald-600 hover:bg-emerald-500 text-white font-semibold text-xs rounded-lg shadow-xs transition"
                          >
                            <Smartphone className="w-3.5 h-3.5" />
                            Send on my phone
                          </a>
                        )}
                        {whatsappStatus?.connected && (
                          <button
                            id={`send-single-btn-${item.lead_id}`}
                            onClick={() => handleSendSingle(item.lead_id)}
                            disabled={isSending}
                            className="inline-flex items-center gap-1.5 px-4 py-2 bg-slate-800 hover:bg-slate-700 dark:bg-slate-700 dark:hover:bg-slate-600 text-white font-semibold text-xs rounded-lg shadow-xs transition disabled:opacity-50"
                          >
                            <Send className="w-3.5 h-3.5" />
                            {isSending ? 'Sending...' : 'Send from here'}
                          </button>
                        )}
                      </>
                    )}
                  </div>
                </div>

                {notes[item.lead_id] && (
                  <p
                    id={`send-note-${item.lead_id}`}
                    className="mt-2 text-[11px] font-medium text-slate-600 dark:text-slate-300"
                  >
                    {notes[item.lead_id]}
                  </p>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {/* Confirmation Modal for Send All */}
      {showConfirmModal && (
        <div id="send-all-confirm-modal" className="fixed inset-0 bg-slate-900/60 backdrop-blur-xs z-50 flex items-center justify-center p-4">
          <div className="max-w-md w-full bg-white dark:bg-slate-900 p-6 rounded-xl shadow-2xl border border-slate-200 dark:border-slate-800 space-y-4">
            <h3 className="text-base font-bold text-slate-900 dark:text-slate-100 flex items-center gap-2">
              <Send className="w-5 h-5 text-emerald-600" /> Confirm Batch WhatsApp Outreach
            </h3>
            <p className="text-xs text-slate-600 dark:text-slate-300 leading-relaxed">
              You are about to transmit <strong>{pendingQueue.length} custom WhatsApp messages</strong> with site preview links via your local relay app socket.
            </p>
            <div className="p-3 bg-slate-50 dark:bg-slate-800 rounded-lg text-xs font-mono text-slate-700 dark:text-slate-300">
              Target Count: {pendingQueue.length} leads
              <br />
              Estimated Spend: $0.00
            </div>
            <div className="flex justify-end gap-2 pt-2">
              <button
                onClick={() => setShowConfirmModal(false)}
                className="px-4 py-2 text-xs font-semibold text-slate-600 dark:text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-800 rounded-md"
              >
                Cancel
              </button>
              <button
                id="confirm-batch-send-btn"
                onClick={handleSendAll}
                className="px-5 py-2 text-xs font-bold text-white bg-emerald-600 hover:bg-emerald-500 rounded-md shadow-xs"
              >
                Confirm &amp; Send Batch Now
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
