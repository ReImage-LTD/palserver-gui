import { useEffect, useState } from "react";
import { getLang } from "./i18n";

/**
 * 公告來源:GitHub repo 裡的 announcement.md,讓公告能推送給所有使用者而不必重新
 * 打包。採跟 promoConfig 相同的三層 fallback:localStorage 快取 -> 遠端 GitHub raw
 * -> 內建的 /announcement.md。
 *
 * 檔案可放「多則」公告,彼此用一行 `===` 分隔;每則有自己的 frontmatter(id/title)。
 * 彈窗會把「尚未看過」的公告依檔案順序一則一則顯示,依 id 記錄已看(localStorage),
 * 所以發佈新 id 就會對所有人重新跳出。
 */

export interface Announcement {
  id: string;
  title: string;
  body: string; // markdown
  /** frontmatter `enabled: false` 可手動關閉一則公告(不用刪掉)。預設開啟。 */
  enabled?: boolean;
  /** frontmatter `until: YYYY-MM-DD`:過了這天就自動不再顯示。 */
  until?: string;
  /** frontmatter `lang: zh|zh-CN|en|ja`:只對該介面語言顯示;省略 = 所有語言都顯示。 */
  lang?: string;
}

/** 是否還要顯示這則公告(未手動關閉、未過期、且符合目前介面語言)。 */
export function isActive(a: Announcement): boolean {
  if (a.enabled === false) return false;
  if (a.until && new Date().toISOString().slice(0, 10) > a.until) return false;
  const current = getLang();
  const announcementLang = current === "no" || current === "sv" ? "en" : current;
  if (a.lang && a.lang !== announcementLang) return false;
  return true;
}

const REMOTE_URL =
  "https://raw.githubusercontent.com/io-software-ai/palserver-gui/main/announcement.md";
const LOCAL_URL = "/announcement.md";
const CACHE_KEY = "palserver.announcements";
const SEEN_KEY = "palserver.announcementsSeen";

/** 解析單則:以 `---` 包住的 frontmatter(id/title)+ markdown 內文。 */
function parseOne(raw: string): Announcement | null {
  const m = raw.match(/^﻿?---\s*\n([\s\S]*?)\n---\s*\n?([\s\S]*)$/);
  if (!m) return null;
  const meta: Record<string, string> = {};
  for (const line of m[1].split("\n")) {
    const kv = line.match(/^([A-Za-z0-9_-]+)\s*:\s*(.*)$/);
    if (kv) meta[kv[1].trim()] = kv[2].trim().replace(/^["']|["']$/g, "");
  }
  if (!meta.id) return null;
  return {
    id: meta.id,
    title: meta.title ?? "公告",
    body: m[2].trim(),
    enabled: meta.enabled !== "false",
    until: meta.until || undefined,
    lang: meta.lang || undefined,
  };
}

/** 用一行 `===` 分隔多則公告,逐則解析。 */
function parseAll(raw: string): Announcement[] {
  return raw
    .split(/^\s*={3,}\s*$/m)
    .map((chunk) => parseOne(chunk.trim()))
    .filter((a): a is Announcement => a !== null);
}

function readCache(): Announcement[] | null {
  try {
    const raw = localStorage.getItem(CACHE_KEY);
    const list = raw ? (JSON.parse(raw) as Announcement[]) : null;
    return Array.isArray(list) ? list : null;
  } catch {
    return null;
  }
}

export function seenIds(): Set<string> {
  try {
    return new Set(JSON.parse(localStorage.getItem(SEEN_KEY) ?? "[]") as string[]);
  } catch {
    return new Set();
  }
}

export function markSeen(id: string): void {
  const ids = seenIds();
  ids.add(id);
  localStorage.setItem(SEEN_KEY, JSON.stringify([...ids]));
}

let shared: Announcement[] = readCache() ?? [];
let fetched = false;
const listeners = new Set<(a: Announcement[]) => void>();

function publish(list: Announcement[]) {
  shared = list;
  listeners.forEach((l) => l(list));
}

async function fetchText(url: string, ms: number): Promise<string | null> {
  try {
    const res = await fetch(url, { cache: "no-cache", signal: AbortSignal.timeout(ms) });
    return res.ok ? await res.text() : null;
  } catch {
    return null;
  }
}

async function refresh(): Promise<void> {
  if (fetched) return;
  fetched = true;
  // 1) 遠端(GitHub)— 內容的權威來源。
  const remote = await fetchText(REMOTE_URL, 6000);
  if (remote !== null) {
    const list = parseAll(remote);
    localStorage.setItem(CACHE_KEY, JSON.stringify(list));
    publish(list);
    return;
  }
  // 2) 一開始沒有快取 -> 改用內建的本地副本。
  if (!readCache()) {
    const local = await fetchText(LOCAL_URL, 4000);
    if (local !== null) publish(parseAll(local));
  }
}

/** 回傳目前所有公告(依檔案順序)。彈窗自己挑出尚未看過的依序顯示。 */
export function useAnnouncements(): Announcement[] {
  const [list, setList] = useState<Announcement[]>(shared);
  useEffect(() => {
    listeners.add(setList);
    void refresh();
    setList(shared);
    return () => {
      listeners.delete(setList);
    };
  }, []);
  return list;
}
