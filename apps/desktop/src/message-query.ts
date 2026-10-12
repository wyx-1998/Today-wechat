export type MessageFilter = {from: string; to: string; sender: string};
export const emptyMessageFilter = (): MessageFilter => ({from: "", to: "", sender: ""});
export function messageQuery(filter: MessageFilter): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  const midnight = (value: string) => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) throw new Error("请选择有效的日期。");
    const [year, month, day] = value.split("-").map(Number);
    const date = new Date(value + "T00:00:00");
    if (Number.isNaN(date.getTime()) || date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day) throw new Error("请选择有效的日期。");
    return date;
  };
  if (filter.from && filter.to && filter.from > filter.to) throw new Error("开始日期不能晚于结束日期，请调整后再查找。");
  if (filter.from) result.time_start = midnight(filter.from).toISOString();
  if (filter.to) {
    const end = midnight(filter.to); end.setDate(end.getDate() + 1);
    if (end.getFullYear() > 9999) throw new Error("结束日期超出支持范围。");
    result.time_end = end.toISOString();
  }
  if (filter.sender === "unknown:") result.sender_unknown = true;
  else if (filter.sender.startsWith("id:")) result.sender_id = filter.sender.slice(3);
  return result;
}
export const messageDay = (date: string) => new Date(date).toLocaleDateString("zh-CN");
export const messageTime = (date: string) => new Date(date).toLocaleTimeString("zh-CN", {hour12:false, hour:"2-digit", minute:"2-digit"});
