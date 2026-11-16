/**
 * There is no account system: the first request from a browser creates its
 * local, device-scoped identity automatically. Seeding a user would contradict
 * that model, so this script only reports the current policy.
 */
console.log(
  'Conversor Generator: sessão local por navegador (cookie + device id). ' +
    'Nenhuma conta é criada ou semeada.',
);
