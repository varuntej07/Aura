/// Canonicalizes device-provided IANA timezone identifiers before persistence.
String canonicalizeTimezoneIdentifier(String identifier) {
  final trimmed = identifier.trim();
  return switch (trimmed) {
    'Asia/Calcutta' => 'Asia/Kolkata',
    _ when trimmed.isEmpty => 'UTC',
    _ => trimmed,
  };
}
