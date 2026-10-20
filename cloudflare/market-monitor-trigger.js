// Cloudflare Worker: market-monitor-trigger
// 唯一职责：在美东 20:15 / 20:45 各派发一次 ensure-morning.yml。
// 「要不要真跑、数据齐不齐、是不是已有任务在跑」全部交给 GitHub 侧 monitor.py --ensure-schedule-run 判断，
// Worker 不再读 status.json / runs 列表（以前两边各写一套判断，Worker 那套还不看当日数据齐不齐）。
//
// Cron（只需 1 条，覆盖夏/冬令时共 4 个时点，错季节的由下面的美东时间闸门丢掉）：
//   15,45 0,1 * * TUE-SAT
// Secret：GITHUB_TOKEN（细粒度令牌，仅 nixhuang/market-monitor，Actions: Read and write）

const REPO = "nixhuang/market-monitor";
const WORKFLOW_URL = `https://api.github.com/repos/${REPO}/actions/workflows/ensure-morning.yml/dispatches`;
const NY_FORMAT = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York", weekday: "short", year: "numeric",
  month: "2-digit", day: "2-digit", hour: "2-digit",
  minute: "2-digit", hourCycle: "h23",
});

function newYork(date) {
  return Object.fromEntries(NY_FORMAT.formatToParts(date).map(({ type, value }) => [type, value]));
}

export default {
  async fetch() {
    return new Response("Not found", { status: 404 });
  },

  async scheduled(event, env) {
    const planned = newYork(new Date(event.scheduledTime));
    const actual = newYork(new Date());
    const plannedDay = `${planned.year}-${planned.month}-${planned.day}`;
    const actualDay = `${actual.year}-${actual.month}-${actual.day}`;
    const minute = Number(planned.minute);

    // 只放行美东周一~周五 20:15 / 20:45；错季节（落在 19:xx / 21:xx）和被拖过 21:00 的投递直接丢掉。
    if (!["Mon", "Tue", "Wed", "Thu", "Fri"].includes(planned.weekday) ||
        planned.hour !== "20" || ![15, 45].includes(minute) ||
        actualDay !== plannedDay || actual.hour !== "20") {
      console.log(`skip: cron=${event.cron} planned NY ${plannedDay} ${planned.hour}:${planned.minute}`);
      return;
    }
    if (!env.GITHUB_TOKEN) throw new Error("Missing GITHUB_TOKEN secret");

    const response = await fetch(WORKFLOW_URL, {
      method: "POST",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        "Content-Type": "application/json",
        "User-Agent": "market-monitor-trigger",
        "X-GitHub-Api-Version": "2022-11-28",
      },
      body: JSON.stringify({ ref: "main" }),
    });
    if (response.status !== 204) {
      // 抛错 → Cloudflare「Cron 事件」里显示为 exception，带上 GitHub 的原因（401=令牌过期/无效，403/404=权限不够）
      const body = (await response.text()).slice(0, 200);
      throw new Error(`dispatch failed HTTP ${response.status}: ${body}`);
    }
    console.log(`dispatched ensure-morning for NY ${plannedDay} 20:${planned.minute}`);
  },
};
