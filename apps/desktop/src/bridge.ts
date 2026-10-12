import { invoke } from "@tauri-apps/api/core";

export async function request<T>(
  command: string,
  params: Record<string, unknown> = {},
): Promise<T> {
  return invoke<T>("local_request", { command, params });
}

export function explainError(error: unknown): string {
  if (
    typeof error === "object" &&
    error &&
    "message" in error &&
    typeof error.message === "string"
  )
    return error.message;
  return "无法联系本地读取程序，请重开应用；已保存数据不会删除。";
}
