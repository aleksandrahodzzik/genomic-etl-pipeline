"""Record docs/images/demo.gif: the web UI processing the `make demo` dataset.

    make up && make gif

A headless Chromium driven by Playwright opens the UI, picks the two files of `make demo`
(data/demo: 200,000 records, so two 100k-record chunks) through the page's own file pickers,
starts the job, follows it through upload -> split -> chunks -> finalize, shows the
statistics and filters the variant table by chromosome and type. Nothing on the page is
mocked: every number in the GIF comes from the running stack.

Timing: the GIF plays at real speed except while the job runs, which is fast-forwarded so
the whole clip stays under ~20 s; those frames carry an "Nx speed" badge, and the page's own
status line ("Completed in N s") shows the real duration. The moments when Playwright hands
the files to the browser are cut, since they are recording overhead, not the app.

Frames come from Chromium's screencast (a frame whenever the page repaints, with its
timestamp). A small drawn cursor shows where the clicks go; headless browsers paint none.

The VCF gets one extra `##` meta line with a random id before upload: jobs are deduplicated
by content, and without it a second recording would show "already processed" instead of a
run. The recorded job is deleted afterwards (`--keep-job` keeps it).

Playwright and Pillow are only needed here, so they are not project dependencies:
    uv run --with playwright==1.64.1 playwright install chromium
    uv run --with playwright==1.64.1 --with pillow==13.0.0 python scripts/record_ui_gif.py
"""

import argparse
import asyncio
import base64
import bisect
import io
import itertools
import math
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from playwright.async_api import Page, async_playwright

ROOT = Path(__file__).resolve().parents[1]

# A drawn cursor and click ring, injected into the page (headless Chromium paints neither).
CURSOR_JS = """
window.addEventListener("DOMContentLoaded", () => {
  const style = document.createElement("style");
  style.textContent = `
    #rec-cursor { position: fixed; left: -40px; top: -40px; width: 22px; height: 22px;
                  z-index: 2147483647; pointer-events: none; transition: opacity .25s; }
    #rec-cursor.off { opacity: 0; }
    #rec-cursor svg { width: 22px; height: 22px; display: block;
                      filter: drop-shadow(0 1px 1.5px rgba(0,0,0,.35)); }
    #rec-ring { position: fixed; width: 36px; height: 36px; margin: -18px 0 0 -18px;
                border-radius: 50%; border: 2px solid #1f3a5f; opacity: 0;
                pointer-events: none; z-index: 2147483646; }
    #rec-ring.on { animation: rec-ring .5s ease-out; }
    @keyframes rec-ring { from { opacity: .9; transform: scale(.35); }
                          to { opacity: 0; transform: scale(1.3); } }`;
  document.head.appendChild(style);
  const cursor = document.createElement("div");
  cursor.id = "rec-cursor";
  cursor.innerHTML = '<svg viewBox="0 0 24 24"><path d="M4 2.5l15 11-6.6.9 3.9 6.9-2.6 1.4' +
    '-3.9-6.9L4.8 20z" fill="#fff" stroke="#132033" stroke-width="1.5" ' +
    'stroke-linejoin="round"/></svg>';
  const ring = document.createElement("div");
  ring.id = "rec-ring";
  document.body.append(cursor, ring);
  document.addEventListener("mousemove", (e) => {
    cursor.style.left = e.clientX - 4 + "px";
    cursor.style.top = e.clientY - 2 + "px";
  }, true);
  window.__recRing = (x, y) => {
    ring.style.left = x + "px"; ring.style.top = y + "px";
    ring.classList.remove("on"); void ring.offsetWidth; ring.classList.add("on");
  };
  window.__recCursor = (on) => cursor.classList.toggle("off", !on);
  document.addEventListener("mousedown", (e) => window.__recRing(e.clientX, e.clientY), true);
});
"""

# Scroll with a fixed duration and easing, so the GIF does not depend on the browser's
# own smooth-scroll speed.
SCROLL_JS = """([selector, offset, ms]) => new Promise((done) => {
  const el = document.querySelector(selector);
  const y1 = Math.max(0, el.getBoundingClientRect().top + window.scrollY - offset);
  const y0 = window.scrollY, t0 = performance.now();
  const step = (t) => {
    const p = Math.min(1, (t - t0) / ms);
    const e = p < .5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2;
    window.scrollTo(0, y0 + (y1 - y0) * e);
    if (p < 1) requestAnimationFrame(step); else done();
  };
  requestAnimationFrame(step);
})"""

CUT = math.inf  # playback speed of a stretch that is left out of the GIF


class Recorder:
    """Collects screencast frames and the playback speed of each stretch of time."""

    def __init__(self) -> None:
        self.frames: list[tuple[float, bytes]] = []
        self.speeds: list[tuple[float, float]] = []  # (from time, speed); FAST = fast-forward
        self.stopped = 0.0

    async def start(self, page: Page) -> None:
        self.cdp = await page.context.new_cdp_session(page)

        async def on_frame(params: dict[str, Any]) -> None:
            stamp = params["metadata"].get("timestamp") or time.time()
            self.frames.append((stamp, base64.b64decode(params["data"])))
            await self.cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})

        self.cdp.on("Page.screencastFrame", lambda p: asyncio.ensure_future(on_frame(p)))
        await self.cdp.send("Page.startScreencast", {"format": "png", "everyNthFrame": 1})
        self.speed(1.0)

    def speed(self, value: float) -> None:
        self.speeds.append((time.time(), value))

    async def stop(self) -> None:
        self.stopped = time.time()
        await self.cdp.send("Page.stopScreencast")
        await asyncio.sleep(0.2)  # let in-flight frame callbacks finish


FAST = -1.0  # marker: fast-forward, the factor is chosen once the stretch's length is known


class Pointer:
    """Moves the real mouse in small steps so the drawn cursor glides instead of jumping."""

    def __init__(self, page: Page, x: float, y: float, park: tuple[float, float]) -> None:
        self.page = page
        self.x, self.y = x, y
        self.parking = park

    async def glide(self, x: float, y: float, seconds: float = 0.5) -> None:
        steps = max(2, int(seconds * 60))
        x0, y0 = self.x, self.y
        for i in range(1, steps + 1):
            p = i / steps
            e = 2 * p * p if p < 0.5 else 1 - (-2 * p + 2) ** 2 / 2
            await self.page.mouse.move(x0 + (x - x0) * e, y0 + (y - y0) * e)
            await asyncio.sleep(seconds / steps)
        self.x, self.y = x, y

    async def to(self, selector: str, seconds: float = 0.5) -> tuple[float, float]:
        box = await self.page.locator(selector).bounding_box()
        assert box, f"{selector} is not visible"
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        await self.page.evaluate("window.__recCursor(true)")
        await self.glide(x, y, seconds)
        return x, y

    async def park(self) -> None:
        """Hide the cursor in the right margin, outside every card (so nothing hovers)."""
        await self.page.evaluate("window.__recCursor(false)")
        await self.glide(*self.parking, 0.3)


async def scroll_to(page: Page, selector: str, offset: int = 16, ms: int = 650) -> None:
    await page.evaluate(SCROLL_JS, [selector, offset, ms])


async def record(args: argparse.Namespace) -> tuple[Recorder, str | None]:
    vcf = (args.data_dir / "variants.vcf").read_bytes()
    first_line_end = vcf.index(b"\n") + 1
    marked_vcf = (
        vcf[:first_line_end] + f"##recording_id={uuid.uuid4()}\n".encode() + vcf[first_line_end:]
    )
    reference = (args.data_dir / "ref.fa").read_bytes()
    job_id: str | None = None

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(
            viewport={"width": args.width, "height": args.height},
            device_scale_factor=1,
            color_scheme="light",
        )
        await context.add_init_script(CURSOR_JS)
        page = await context.new_page()

        async def remember_job(response: Any) -> None:
            nonlocal job_id
            if response.request.method == "POST" and response.url.endswith("/api/v1/jobs"):
                job_id = (await response.json())["id"]

        page.on("response", remember_job)
        await page.goto(args.api.rstrip("/") + "/")
        await page.wait_for_function(
            "document.getElementById('healthText').textContent === 'API online'"
        )
        await page.wait_for_load_state("networkidle")  # the pre-loaded sample has rendered
        pointer = Pointer(page, args.width * 0.55, args.height * 0.62, (args.width - 8, 300))
        await page.mouse.move(pointer.x, pointer.y)

        recorder = Recorder()
        await recorder.start(page)
        await asyncio.sleep(0.7)

        # 1. Pick the two files of `make demo` with the page's file pickers.
        await scroll_to(page, "#uploadCard", offset=24, ms=600)
        for selector, payload in (
            ("#dropVcf", {"name": "variants.vcf", "mimeType": "text/plain", "buffer": marked_vcf}),
            ("#dropRef", {"name": "ref.fa", "mimeType": "text/plain", "buffer": reference}),
        ):
            await pointer.to(selector)
            async with page.expect_file_chooser() as chooser:
                await page.mouse.click(pointer.x, pointer.y)
            recorder.speed(CUT)  # handing 9 MB to the browser is recording overhead
            await (await chooser.value).set_files(payload)
            await page.wait_for_function(
                f"document.querySelector('{selector}').classList.contains('has')"
            )
            recorder.speed(1.0)
            await asyncio.sleep(0.2)

        # 2. Start the job; the page follows it through split -> chunks -> finalize.
        await pointer.to("#btnRun", 0.45)
        await page.mouse.click(pointer.x, pointer.y)
        recorder.speed(FAST)
        await pointer.park()
        await page.wait_for_function(
            "document.getElementById('statusText').textContent.startsWith('Completed')",
            timeout=args.timeout * 1000,
            polling=50,
        )
        recorder.speed(1.0)
        await asyncio.sleep(0.5)  # the completed stepper, before the page scrolls on
        await page.wait_for_function(
            "!document.getElementById('results').classList.contains('hidden')"
            " && document.querySelectorAll('#varBody tr').length > 0",
            timeout=30_000,
        )
        await asyncio.sleep(1.3)  # the page has scrolled the summary into view by itself

        # 3. Statistics.
        await scroll_to(page, "#results > .grid2", offset=16, ms=600)
        await asyncio.sleep(1.3)
        await scroll_to(page, "#paneVar", offset=110, ms=650)
        await asyncio.sleep(0.3)

        # 4. Query: filter the variant table by chromosome, then by type.
        for selector, value in (("#fChrom", "chr2"), ("#fType", "DEL")):
            x, y = await pointer.to(selector, 0.45)
            await page.evaluate("([x, y]) => window.__recRing(x, y)", [x, y])
            async with page.expect_response(lambda r: "/variants?" in r.url):
                await page.select_option(selector, value)
            await asyncio.sleep(0.5)
        await pointer.park()
        await asyncio.sleep(1.0)
        await recorder.stop()
        await browser.close()
    return recorder, job_id


def timeline(recorder: Recorder, tick: float, fast_seconds: float) -> list[tuple[float, float]]:
    """Source times to sample, one per output tick, each with its playback speed."""
    marks = [*recorder.speeds, (recorder.stopped, 1.0)]
    samples: list[tuple[float, float]] = []
    for (start, speed), (end, _) in itertools.pairwise(marks):
        if speed == CUT:
            continue
        if speed == FAST:  # whole multiples, so the badge reads "3x", not "3.27x"
            speed = max(1.0, round((end - start) / fast_seconds))
        t = start
        while t < end:
            samples.append((t, speed))
            t += tick * speed
    return samples


def speed_badge(image: Image.Image, speed: float, font: ImageFont.FreeTypeFont) -> None:
    """A fast-forward pill in the top-right corner of a frame."""
    draw = ImageDraw.Draw(image)
    label = f"{speed:.0f}x speed"
    text_w = draw.textlength(label, font=font)
    w, h = int(text_w) + 52, 30
    x0, y0 = image.width - w - 14, 12
    draw.rounded_rectangle((x0, y0, x0 + w, y0 + h), radius=15, fill=(19, 32, 51))
    for dx in (0, 8):  # two triangles: the fast-forward icon
        ix = x0 + 14 + dx
        draw.polygon([(ix, y0 + 9), (ix, y0 + h - 9), (ix + 8, y0 + h / 2)], fill="white")
    draw.text((x0 + 38, y0 + h / 2), label, font=font, fill="white", anchor="lm")


def build_gif(recorder: Recorder, out: Path, fps: float, fast_seconds: float) -> tuple[float, int]:
    frames = sorted(recorder.frames, key=lambda f: f[0])
    stamps = [stamp for stamp, _ in frames]
    tick = 1 / fps
    picks: list[list[Any]] = []  # [frame index, speed, ticks]
    for t, speed in timeline(recorder, tick, fast_seconds):
        index = max(0, bisect.bisect_right(stamps, t) - 1)
        if picks and picks[-1][0] == index and picks[-1][1] == speed:
            picks[-1][2] += 1
        else:
            picks.append([index, speed, 1])

    font = ImageFont.load_default(size=14)
    images = []
    for index, speed, _ in picks:
        image = Image.open(io.BytesIO(frames[index][1])).convert("RGB")
        if speed > 1:
            speed_badge(image, speed, font)
        images.append(image)
    # One palette for the whole GIF, built from every frame (subsampled without blending, so
    # only real colors go in). Unchanged pixels then stay identical between frames, and each
    # frame only stores the rectangle that changed.
    small = [im.resize((im.width // 4, im.height // 4), Image.Resampling.NEAREST) for im in images]
    sheet = Image.new("RGB", (small[0].width, small[0].height * len(small)))
    for i, image in enumerate(small):
        sheet.paste(image, (0, i * image.height))
    palette = sheet.quantize(colors=255, method=Image.Quantize.FASTOCTREE)
    quantized = [im.quantize(palette=palette, dither=Image.Dither.NONE) for im in images]
    durations = [round(ticks * tick * 1000) for _, _, ticks in picks]
    out.parent.mkdir(parents=True, exist_ok=True)
    quantized[0].save(
        out, save_all=True, append_images=quantized[1:], duration=durations, loop=0, disposal=1
    )
    return sum(durations) / 1000, len(quantized)


def delete_job(job_id: str) -> None:
    from sqlalchemy import text

    from variant_etl.db.session import get_engine
    from variant_etl.storage.s3 import get_storage

    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job_id})
    get_storage().delete_prefix(f"chunks/{job_id}/")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--data-dir", default=ROOT / "data" / "demo", type=Path)
    parser.add_argument("--out", default=ROOT / "docs" / "images" / "demo.gif", type=Path)
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--height", type=int, default=640)
    parser.add_argument("--fps", type=float, default=12.5)
    parser.add_argument(
        "--fast-seconds",
        type=float,
        default=4.0,
        help="roughly how long the running job takes in the GIF (it is sped up to fit)",
    )
    parser.add_argument("--timeout", type=float, default=300, help="seconds to wait for the job")
    parser.add_argument("--keep-job", action="store_true", help="do not delete the recorded job")
    args = parser.parse_args()
    if not (args.data_dir / "variants.vcf").exists():
        print(f"{args.data_dir}/variants.vcf is missing; run `make demo` first", file=sys.stderr)
        return 1

    recorder, job_id = asyncio.run(record(args))
    seconds, count = build_gif(recorder, args.out, args.fps, args.fast_seconds)
    size = args.out.stat().st_size / 2**20
    print(f"wrote {args.out}: {seconds:.1f} s, {count} frames, {size:.1f} MiB")
    if job_id and not args.keep_job:
        try:
            delete_job(job_id)
            print(f"deleted the recorded job {job_id}")
        except Exception as exc:  # cleanup is best effort: the GIF is already written
            print(f"could not delete job {job_id} ({exc}); it stays in the database")
    return 0


if __name__ == "__main__":
    sys.exit(main())
