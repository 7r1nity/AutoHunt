export type ApplicationExportFormat = "csv" | "xlsx";
export type ApplicationExportScope = "all" | "board";

export function applicationExportFilename(disposition: string | null, format: ApplicationExportFormat, scope: ApplicationExportScope): string {
  const fallback = `autohunt-applications-${scope}.${format}`;
  const extended = disposition?.match(/filename\*\s*=\s*UTF-8'[^']*'([^;]+)/i)?.[1];
  const quoted = disposition?.match(/filename\s*=\s*(?:"([^"]*)"|([^;]*))/i);
  let candidate = quoted?.[1] ?? quoted?.[2] ?? "";
  if (extended) {
    try { candidate = decodeURIComponent(extended.trim()); }
    catch { return fallback; }
  }
  // A server filename is a label, never a filesystem path. Keep Unicode names
  // while removing control characters and Windows-reserved filename symbols.
  const leaf = candidate.split(/[\\/]/).at(-1) ?? "";
  const cleaned = Array.from(leaf.replace(/[\u0000-\u001f\u007f<>:"|?*]/g, "_").trim()).slice(0, 160).join("");
  if (!cleaned || /^\.+$/.test(cleaned)) return fallback;
  return cleaned.replace(/\.(?:csv|xlsx)$/i, "") + `.${format}`;
}
