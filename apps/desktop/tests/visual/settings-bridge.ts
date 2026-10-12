export async function request<T>(command: string, params: Record<string, unknown> = {}): Promise<T> {
  const response=await fetch("/api",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({command,params})});
  const body=await response.json();
  if (!response.ok) throw new Error(body.error.message);
  return body.result;
}
export const explainError=(error:unknown)=>error instanceof Error?error.message:"隔离请求失败";
