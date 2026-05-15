import '../services/firestore_service.dart';

/// Hardcoded fallback pills shown when Firestore hasn't been populated yet
/// (before the first 7 AM scheduled run) or when the fetch fails.
const Map<String, List<String>> _fallbackSuggestionPillsByAgentId = {
  'sports': [
    'IPL scores today',
    'Top performer today',
    'Tournament standings',
    'Match highlights',
    'Player stats',
  ],
  'technews': [
    'Latest AI news',
    'Top tech story',
    'This week in LLMs',
    'Startup funding',
    'Open source picks',
  ],
  'jobs': [
    'SWE jobs today',
    'Remote roles',
    'Top companies hiring',
    'Resume tips',
    'Interview prep',
  ],
  'posts': [
    'Draft a tweet',
    'LinkedIn post idea',
    'Thread starter',
    'Trending angle',
    'Contrarian take',
  ],
};

/// Reads agent suggestion pills from `agent_suggestion_pills/{uid}` in Firestore.
/// Always returns a non-empty list — falls back to hardcoded defaults on any error
/// so the UI always has something to show.
class AgentSuggestionPillsRepository {
  final FirestoreService _firestoreService;

  const AgentSuggestionPillsRepository({required FirestoreService firestoreService})
      : _firestoreService = firestoreService;

  Future<List<String>> fetchSuggestionPillsForAgent(
    String uid,
    String agentId,
  ) async {
    final result = await _firestoreService.getDocument(
      'agent_suggestion_pills',
      uid,
      (data) => data,
    );
    return result.when(
      success: (data) {
        final raw = data[agentId];
        if (raw is List && raw.isNotEmpty) return raw.cast<String>();
        return _fallbackPillsForAgent(agentId);
      },
      failure: (_) => _fallbackPillsForAgent(agentId),
    );
  }

  List<String> _fallbackPillsForAgent(String agentId) =>
      _fallbackSuggestionPillsByAgentId[agentId] ?? [];
}
