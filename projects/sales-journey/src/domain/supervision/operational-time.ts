export const SUPERVISION_OPERATIONAL_TIME_ZONE = "Asia/Taipei";

/** Returns the store's calendar day without relying on the server's local timezone. */
export function supervisionOperationalDate(
  timestamp: string,
  timeZone = SUPERVISION_OPERATIONAL_TIME_ZONE,
): string {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) {
    throw new RangeError("timestamp must be an ISO timestamp");
  }
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(date);
  const value = (type: "year" | "month" | "day") => (
    parts.find((part) => part.type === type)?.value
  );
  const year = value("year");
  const month = value("month");
  const day = value("day");
  if (!year || !month || !day) throw new RangeError("timeZone cannot produce a calendar day");
  return `${year}-${month}-${day}`;
}
