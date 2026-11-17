import { readFile } from "node:fs/promises";
import path from "node:path";
import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const FILES_ROOT = process.env.DEEPTUTOR_FILES_DIR
  ?? path.join(process.cwd(), "..", "backend", "data", "files");

const MIME: Record<string, string> = {
  ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
  ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
  ".pdf": "application/pdf", ".mp3": "audio/mpeg", ".wav": "audio/wav",
  ".mp4": "video/mp4", ".webm": "video/webm",
  ".txt": "text/plain; charset=utf-8", ".md": "text/markdown; charset=utf-8",
  ".html": "text/html; charset=utf-8", ".json": "application/json",
  ".zip": "application/zip",
};

/** GET /files/<path> — serves agent outputs/attachments from the backend
 *  data dir (replaces the removed Python static mount). */
export async function GET(
  _req: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path: segments } = await params;
  const rel = segments.join("/");
  const resolved = path.resolve(FILES_ROOT, rel);
  if (!resolved.startsWith(path.resolve(FILES_ROOT) + path.sep) && resolved !== path.resolve(FILES_ROOT)) {
    return new NextResponse("Forbidden", { status: 403 });
  }
  try {
    const data = await readFile(resolved);
    const ext = path.extname(resolved).toLowerCase();
    return new NextResponse(new Uint8Array(data), {
      headers: {
        "content-type": MIME[ext] ?? "application/octet-stream",
        "cache-control": "private, max-age=60",
      },
    });
  } catch {
    return new NextResponse("Not Found", { status: 404 });
  }
}
