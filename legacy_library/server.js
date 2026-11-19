'use strict';

/*
 * Pinecrest Library lending service, 2011 build.
 *
 * A small JSON-over-HTTP service for the branch lending desk. Members borrow
 * titles from a 60-book catalogue, return them, and pay late fees. Written
 * against Node's standard library only; all state lives in memory and starts
 * empty (the catalogue is fixed).
 *
 * Environment: PORT (default 8080), HOST (default 127.0.0.1).
 */

const http = require('http');
const crypto = require('crypto');

const CATALOGUE_SIZE = 60;
const PER_PAGE_DEFAULT = 10;
const PER_PAGE_MAX = 50;
const MAX_ACTIVE_LOANS = 5;
const FEE_BLOCK_CENTS = 500;
const LOAN_DAYS = { standard: 14, premium: 28 };
const GRACE_DAYS = 2;
const CENTS_PER_LATE_DAY = 25;
const FEE_CAP_CENTS = 1000;
const DAY_MS = 24 * 60 * 60 * 1000;
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

const ADJECTIVES = [
  'Silent', 'Gilded', 'Forgotten', 'Distant', 'Crimson',
  'Quiet', 'Hollow', 'Windward', 'Amber', 'Restless',
];
const NOUNS = [
  'Harbour', 'Ledger', 'Orchard', 'Lantern', 'Compass', 'Almanac',
  'Garden', 'Atlas', 'Cartographer', 'Meadow', 'Chronicle', 'Parlour',
];

// ISBN-13 check digit over the first 12 digits: weights 1, 3, 1, 3, ...
function isbnCheckDigit(twelve) {
  let sum = 0;
  for (let k = 0; k < twelve.length; k++) {
    sum += Number(twelve[k]) * (k % 2 === 0 ? 1 : 3);
  }
  return String((10 - (sum % 10)) % 10);
}

function buildCatalogue() {
  const books = [];
  for (let i = 1; i <= CATALOGUE_SIZE; i++) {
    const core = '9780' + String(i).padStart(8, '0');
    books.push({
      isbn: core + isbnCheckDigit(core),
      title: `${ADJECTIVES[i % ADJECTIVES.length]} ${NOUNS[(i * 7) % NOUNS.length]}`,
      available: true,
    });
  }
  return books;
}

const catalogue = buildCatalogue();
const booksByIsbn = new Map(catalogue.map((book) => [book.isbn, book]));
const members = new Map();
const loans = new Map();

function newId(prefix, bytes, existing) {
  let id;
  do {
    id = prefix + crypto.randomBytes(bytes).toString('hex');
  } while (existing.has(id));
  return id;
}

// Dates are YYYY-MM-DD, read as UTC calendar days. Returns the day number, or
// null when the text is missing or is not a real calendar date.
function parseDay(text) {
  if (typeof text !== 'string' || !DATE_PATTERN.test(text)) return null;
  const ms = Date.parse(text + 'T00:00:00Z');
  if (Number.isNaN(ms) || new Date(ms).toISOString().slice(0, 10) !== text) return null;
  return ms / DAY_MS;
}

function formatDay(day) {
  return new Date(day * DAY_MS).toISOString().slice(0, 10);
}

function money(cents) {
  return (cents / 100).toFixed(2);
}

function lateFeeCents(daysLate) {
  if (daysLate <= GRACE_DAYS) return 0;
  return Math.min(CENTS_PER_LATE_DAY * daysLate, FEE_CAP_CENTS);
}

// parseInt semantics: "7.9" is 7, "5abc" is 5; anything unparsable (or missing) is the fallback.
function intParam(params, name, fallback) {
  const value = parseInt(params.get(name), 10);
  return Number.isNaN(value) ? fallback : value;
}

function normaliseIsbn(text) {
  return (text ?? '').replace(/[\s-]/g, '').toUpperCase();
}

function decodeSegment(segment) {
  try {
    return decodeURIComponent(segment);
  } catch (err) {
    return segment;
  }
}

function send(res, status, body) {
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(body));
}

function fail(res, status, code) {
  send(res, status, { error: code });
}

function memberView(member) {
  return {
    member_id: member.member_id,
    name: member.name,
    tier: member.tier,
    active_loans: member.active_loans,
    fees_due: money(member.fees_cents),
    created_at: member.created_at,
  };
}

function bookView(book) {
  return { isbn: book.isbn, title: book.title, available: book.available };
}

function loanView(loan) {
  const view = {
    loan_id: loan.loan_id,
    member_id: loan.member_id,
    isbn: loan.isbn,
    title: loan.title,
    borrowed_on: loan.borrowed_on,
    due_on: loan.due_on,
    status: loan.status,
  };
  if (loan.receipt) {
    view.returned_on = loan.receipt.returned_on;
    view.days_late = loan.receipt.days_late;
    view.fee = loan.receipt.fee;
  }
  return view;
}

function createMember(params, res) {
  const name = (params.get('name') ?? '').trim();
  if (!name) return fail(res, 400, 'NAME_REQUIRED');
  const tier = (params.get('tier') ?? '').trim().toLowerCase() === 'premium' ? 'premium' : 'standard';
  const member = {
    member_id: newId('m_', 4, members),
    name,
    tier,
    active_loans: 0,
    fees_cents: 0,
    created_at: new Date().toISOString(),
  };
  members.set(member.member_id, member);
  send(res, 201, memberView(member));
}

function showMember(memberId, res) {
  const member = members.get(memberId);
  if (!member) return fail(res, 404, 'MEMBER_NOT_FOUND');
  send(res, 200, memberView(member));
}

function listBooks(params, res) {
  const perPage = Math.min(PER_PAGE_MAX, Math.max(1, intParam(params, 'per_page', PER_PAGE_DEFAULT)));
  const page = Math.max(1, intParam(params, 'page', 1));
  const start = (page - 1) * perPage;
  send(res, 200, {
    page,
    per_page: perPage,
    total: catalogue.length,
    items: catalogue.slice(start, start + perPage).map(bookView),
  });
}

function showBook(isbnParam, res) {
  const book = booksByIsbn.get(normaliseIsbn(isbnParam));
  if (!book) return fail(res, 404, 'BOOK_NOT_FOUND');
  send(res, 200, bookView(book));
}

function borrow(params, res) {
  const member = members.get(params.get('member_id'));
  if (!member) return fail(res, 404, 'MEMBER_NOT_FOUND');
  const borrowedText = params.get('borrowed_on');
  const borrowedDay = parseDay(borrowedText);
  if (borrowedDay === null) return fail(res, 400, 'INVALID_DATE');
  const book = booksByIsbn.get(normaliseIsbn(params.get('isbn')));
  if (!book) return fail(res, 404, 'BOOK_NOT_FOUND');
  if (member.fees_cents >= FEE_BLOCK_CENTS) return fail(res, 403, 'FEES_OUTSTANDING');
  if (member.active_loans >= MAX_ACTIVE_LOANS) return fail(res, 409, 'LOAN_LIMIT');
  if (!book.available) return fail(res, 409, 'BOOK_UNAVAILABLE');

  const loan = {
    loan_id: newId('L-', 3, loans),
    member_id: member.member_id,
    isbn: book.isbn,
    title: book.title,
    borrowed_on: borrowedText,
    due_on: formatDay(borrowedDay + LOAN_DAYS[member.tier]),
    status: 'active',
    receipt: null,
  };
  loans.set(loan.loan_id, loan);
  book.available = false;
  member.active_loans += 1;
  send(res, 201, loanView(loan));
}

function showLoan(loanId, res) {
  const loan = loans.get(loanId);
  if (!loan) return fail(res, 404, 'LOAN_NOT_FOUND');
  send(res, 200, loanView(loan));
}

function returnLoan(loanId, params, res) {
  const loan = loans.get(loanId);
  if (!loan) return fail(res, 404, 'LOAN_NOT_FOUND');
  const returnedText = params.get('returned_on');
  const returnedDay = parseDay(returnedText);
  if (returnedDay === null) return fail(res, 400, 'INVALID_DATE');
  if (returnedDay < parseDay(loan.borrowed_on)) return fail(res, 400, 'RETURN_BEFORE_BORROW');
  if (loan.receipt) return send(res, 200, loan.receipt);

  const daysLate = Math.max(0, returnedDay - parseDay(loan.due_on));
  const feeCents = lateFeeCents(daysLate);
  loan.status = 'returned';
  loan.receipt = {
    loan_id: loan.loan_id,
    returned_on: returnedText,
    days_late: daysLate,
    fee: money(feeCents),
    status: 'returned',
  };
  booksByIsbn.get(loan.isbn).available = true;
  const member = members.get(loan.member_id);
  member.active_loans -= 1;
  member.fees_cents += feeCents;
  send(res, 200, loan.receipt);
}

function route(req, res, body) {
  const url = new URL(req.url, 'http://localhost');
  const segments = url.pathname.split('/').filter(Boolean).map(decodeSegment);
  const params = new URLSearchParams(url.search);
  if (req.method === 'POST') {
    for (const [key, value] of new URLSearchParams(body)) params.set(key, value);
  }

  const [head, id, action] = segments;
  const count = segments.length;
  if (req.method === 'POST' && head === 'members' && count === 1) return createMember(params, res);
  if (req.method === 'GET' && head === 'members' && count === 2) return showMember(id, res);
  if (req.method === 'GET' && head === 'books' && count === 1) return listBooks(params, res);
  if (req.method === 'GET' && head === 'books' && count === 2) return showBook(id, res);
  if (req.method === 'POST' && head === 'loans' && count === 1) return borrow(params, res);
  if (req.method === 'GET' && head === 'loans' && count === 2) return showLoan(id, res);
  if (req.method === 'POST' && head === 'loans' && count === 3 && action === 'return') {
    return returnLoan(id, params, res);
  }
  return fail(res, 404, 'NOT_FOUND');
}

const server = http.createServer((req, res) => {
  let body = '';
  req.setEncoding('utf8');
  req.on('data', (chunk) => {
    body += chunk;
  });
  req.on('end', () => route(req, res, body));
});

server.listen(Number(process.env.PORT || 8080), process.env.HOST || '127.0.0.1');
