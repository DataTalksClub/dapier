/* Independent reads: a slow or failed section cannot hold up its siblings.
   A newer refresh supersedes every response from the previous refresh. */
export const overviewSections = ['workflows', 'activity', 'usage', 'connections', 'credentials', 'tokens', 'emails'];

export function createOverviewLoader(request, onSection, onError) {
  let generation = 0;
  return async function load() {
    const current = ++generation;
    const results = await Promise.all(overviewSections.map(async (section) => {
      try {
        const data = await request(`/api/admin/overview?section=${section}`);
        if (current !== generation) return false;
        onSection(section, data);
        return true;
      } catch (error) {
        if (current === generation) onError(section, error);
        return false;
      }
    }));
    return current === generation && results.every(Boolean);
  };
}
