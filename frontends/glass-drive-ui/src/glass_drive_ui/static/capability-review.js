export function equivalentReapprovalScopes(availableScopes = [], reviewItem = null) {
  if (!reviewItem) return null;
  const allowed = new Set(Array.isArray(availableScopes) ? availableScopes.map(String) : []);
  return (Array.isArray(reviewItem.scopes) ? reviewItem.scopes : [])
    .map(String)
    .filter((scope, index, values) => allowed.has(scope) && values.indexOf(scope) === index)
    .sort();
}
