// Pure Denver-time helpers for the daily cron (unit-tested in test/schedule.test.ts).
export const RUN_HOUR = 8; // 8:21 AM America/Denver
export const RUN_MINUTE = 21;

export function denverParts(t: number | Date): { date: string; hour: number; minute: number; offset: string } {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Denver", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZoneName: "short",
  }).formatToParts(new Date(t));
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
  return {
    date: `${get("year")}-${get("month")}-${get("day")}`,
    hour: Number(get("hour")) % 24, // numeric compare: "08" -> 8
    minute: Number(get("minute")),
    offset: get("timeZoneName"), // MDT / MST
  };
}

export type CronDecision = { action: "run" | "check" | "skip"; date: string; reason: string };

/** Two UTC crons fire daily (14:21, 15:21). Exactly one is 8:21 Denver -> run.
 *  The other is 9:21 (MDT) -> check today's run exists, or 7:21 (MST) -> skip. */
export function cronDecision(scheduledTime: number): CronDecision {
  const p = denverParts(scheduledTime);
  const at = `${p.date} ${String(p.hour).padStart(2, "0")}:${String(p.minute).padStart(2, "0")} ${p.offset}`;
  if (p.hour === RUN_HOUR) return { action: "run", date: p.date, reason: `scheduled run at ${at}` };
  if (p.hour === RUN_HOUR + 1) return { action: "check", date: p.date, reason: `post-run check at ${at}` };
  return { action: "skip", date: p.date, reason: `not run hour (${at})` };
}
