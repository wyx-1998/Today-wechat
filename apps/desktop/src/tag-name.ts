export const TAG_NAME_LIMIT = 30;

// Match Python str.strip(), including its extra Unicode whitespace characters.
// Count code points, not UTF-16 units or grapheme clusters, as the store does.
export function validateTagName(value: string) {
  const name = value.replace(
    /^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/gu,
    "",
  );
  const length = Array.from(name).length;
  return { name, length, valid: length > 0 && length <= TAG_NAME_LIMIT };
}
