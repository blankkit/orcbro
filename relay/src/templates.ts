// Fixed texts for every alert shown on the iPhone. The bridge sends only a template and short values
// (job name, printer name, error text); the relay does not accept custom titles or bodies.
// The bridge uses the same texts (bridge/notify.py) when it has its own APNs key.

export const LIMITS = { job: 120, printer: 60, detail: 200 };

export type Language = "en" | "de";
export type Template = "finished" | "failed" | "cancelled" | "paused" | "error";
export const TEMPLATES = new Set<Template>(["finished", "failed", "cancelled", "paused", "error"]);

/** Remove control characters, collapse whitespace and limit length. Return `null` if empty or too long. */
export function clean(value: unknown, limit: number): string | null {
  if (typeof value !== "string") return null;
  const text = value.replace(/[\u0000-\u001f\u007f-\u009f\u2028\u2029]/g, " ").replace(/\s+/g, " ").trim();
  return text && text.length <= limit ? text : null;
}

export function startAlert(job: string, printer: string, language: Language = "en") {
  return language === "de"
    ? { title: "Druck gestartet", body: `${job} auf ${printer}` }
    : { title: "Print Started", body: `${job} on ${printer}` };
}

export function alert(template: Template, job: string, printer: string, detail: string | null, language: Language = "en") {
  if (language === "en") {
    switch (template) {
      case "finished":
        return { title: "Print Finished", body: `${job} on ${printer} is done.` };
      case "failed":
        return { title: "Print Failed", body: detail ? `${job} on ${printer}: ${detail}` : `${job} on ${printer} did not finish.` };
      case "cancelled":
        return { title: "Print Canceled", body: `${job} on ${printer} was canceled.` };
      case "paused":
        return { title: "Print Paused", body: detail ? `${job} on ${printer}: ${detail}` : `${job} on ${printer} is paused.` };
      case "error":
        return { title: `Message from ${printer}`, body: detail ?? "The printer reports an error." };
    }
  }
  switch (template) {
    case "finished":
      return { title: "Druck fertig", body: `${job} auf ${printer} ist fertig.` };
    case "failed":
      return { title: "Druck fehlgeschlagen", body: detail ? `${job} auf ${printer}: ${detail}` : `${job} auf ${printer} wurde nicht fertig.` };
    case "cancelled":
      return { title: "Druck abgebrochen", body: `${job} auf ${printer} wurde abgebrochen.` };
    case "paused":
      return { title: "Druck pausiert", body: detail ? `${job} auf ${printer}: ${detail}` : `${job} auf ${printer} ist pausiert.` };
    case "error":
      return { title: `Meldung von ${printer}`, body: detail ?? "Der Drucker meldet einen Fehler." };
  }
}
