(function () {
  const bookingsRowsEl = document.getElementById('bookings-rows');
  const bookingsFeedbackEl = document.getElementById('bookings-feedback');
  const filterDateEl = document.getElementById('filter-date');
  const filterStatusEl = document.getElementById('filter-status');
  const filterTrailerEl = document.getElementById('filter-trailer');

  const kpiTodayEl = document.getElementById('kpi-today');
  const kpiPendingEl = document.getElementById('kpi-pending');
  const kpiConfirmedEl = document.getElementById('kpi-confirmed');
  const kpiCancelledEl = document.getElementById('kpi-cancelled');

  const blocksRowsEl = document.getElementById('blocks-rows');
  const blocksFeedbackEl = document.getElementById('blocks-feedback');
  const blockFormEl = document.getElementById('block-form');
  const blockTrailerEl = document.getElementById('block-trailer');
  const blockStartEl = document.getElementById('block-start');
  const blockEndEl = document.getElementById('block-end');
  const blockReasonEl = document.getElementById('block-reason');

  const refreshBookingsBtn = document.getElementById('refresh-bookings');
  const refreshBlocksBtn = document.getElementById('refresh-blocks');

  const state = {
    bookings: [],
    blocks: []
  };

  function formatTrailer(type) {
    if (type === 'GALLER') return 'Gallersläp';
    if (type === 'KAP') return 'Kåpsläp';
    return type || '-';
  }

  function formatStatus(status) {
    if (status === 'PENDING_PAYMENT') return 'Pending';
    if (status === 'CONFIRMED') return 'Confirmed';
    if (status === 'CANCELLED') return 'Cancelled';
    return status || '-';
  }

  function statusClass(status) {
    if (status === 'PENDING_PAYMENT') return 'status-pending';
    if (status === 'CONFIRMED') return 'status-confirmed';
    if (status === 'CANCELLED') return 'status-cancelled';
    return '';
  }

  function toDateOnly(isoDatetime) {
    if (!isoDatetime || isoDatetime.length < 10) return '';
    return isoDatetime.slice(0, 10);
  }

  function formatDateTime(isoDatetime) {
    if (!isoDatetime) return '-';
    const value = isoDatetime.length >= 16 ? isoDatetime.slice(0, 16) : isoDatetime;
    return value.replace('T', ' ');
  }

  function formatTimeRange(startDt, endDt) {
    const start = formatDateTime(startDt);
    const end = formatDateTime(endDt);
    if (start === '-' || end === '-') return '-';
    return start + ' - ' + end;
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#39;');
  }

  function computeFilteredBookings() {
    const selectedDate = filterDateEl.value;
    const selectedTrailer = filterTrailerEl.value;
    const selectedStatus = filterStatusEl.value;

    let rows = state.bookings.slice();
    if (selectedDate) {
      rows = rows.filter((row) => toDateOnly(row.startDt) === selectedDate);
    }
    if (selectedTrailer !== 'ALL') {
      rows = rows.filter((row) => row.trailerType === selectedTrailer);
    }
    if (selectedStatus !== 'ALL') {
      rows = rows.filter((row) => row.status === selectedStatus);
    }
    return rows;
  }

  function updateKpis() {
    const todayDate = new Date().toISOString().slice(0, 10);
    const selectedDate = filterDateEl.value;
    const selectedTrailer = filterTrailerEl.value;

    const forStatusCounts = state.bookings.filter((row) => {
      if (selectedDate && toDateOnly(row.startDt) !== selectedDate) return false;
      if (selectedTrailer !== 'ALL' && row.trailerType !== selectedTrailer) return false;
      return true;
    });

    const todayCount = state.bookings.filter((row) => toDateOnly(row.startDt) === todayDate).length;
    const pendingCount = forStatusCounts.filter((row) => row.status === 'PENDING_PAYMENT').length;
    const confirmedCount = forStatusCounts.filter((row) => row.status === 'CONFIRMED').length;
    const cancelledCount = forStatusCounts.filter((row) => row.status === 'CANCELLED').length;

    kpiTodayEl.textContent = String(todayCount);
    kpiPendingEl.textContent = String(pendingCount);
    kpiConfirmedEl.textContent = String(confirmedCount);
    kpiCancelledEl.textContent = String(cancelledCount);
  }

  function renderBookings() {
    const rows = computeFilteredBookings();
    updateKpis();

    if (!rows.length) {
      bookingsRowsEl.innerHTML = '<tr><td colspan="6">Inga bokningar matchar filtret.</td></tr>';
      bookingsFeedbackEl.textContent = '0 bokningar visas.';
      return;
    }

    bookingsRowsEl.innerHTML = rows.map((row) => {
      const reference = row.bookingReference || ('Bokning #' + row.bookingId);
      const detailsHref = '/confirm?bookingId=' + encodeURIComponent(row.bookingId);
      return '<tr>' +
        '<td>' + escapeHtml(reference) + '</td>' +
        '<td>' + escapeHtml(formatTrailer(row.trailerType)) + '</td>' +
        '<td>' + escapeHtml(formatTimeRange(row.startDt, row.endDt)) + '</td>' +
        '<td><span class="status-pill ' + escapeHtml(statusClass(row.status)) + '">' + escapeHtml(formatStatus(row.status)) + '</span></td>' +
        '<td>' + escapeHtml(String(row.price)) + ' kr</td>' +
        '<td><a class="link" href="' + detailsHref + '" target="_blank" rel="noopener">Öppna detaljer</a></td>' +
      '</tr>';
    }).join('');
    bookingsFeedbackEl.textContent = rows.length + ' bokningar visas.';
  }

  async function fetchBookings() {
    bookingsFeedbackEl.textContent = 'Laddar bokningar...';
    try {
      const response = await fetch('/api/admin/bookings', { cache: 'no-store' });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data && data.error ? data.error : 'Kunde inte ladda bokningar');
      }
      state.bookings = Array.isArray(data.bookings) ? data.bookings : [];
      renderBookings();
    } catch (error) {
      bookingsFeedbackEl.textContent = 'Fel: ' + error.message;
      bookingsRowsEl.innerHTML = '';
      updateKpis();
    }
  }

  function renderBlocks() {
    const rows = state.blocks.slice().sort((a, b) => {
      return String(a.startDatetime || '').localeCompare(String(b.startDatetime || ''));
    });

    if (!rows.length) {
      blocksRowsEl.innerHTML = '<tr><td colspan="6">Inga aktiva blockeringar.</td></tr>';
      return;
    }

    blocksRowsEl.innerHTML = rows.map((row) => {
      const reason = row.reason || '-';
      return '<tr>' +
        '<td>' + escapeHtml(String(row.id)) + '</td>' +
        '<td>' + escapeHtml(formatTrailer(row.trailerType)) + '</td>' +
        '<td>' + escapeHtml(formatDateTime(row.startDatetime)) + '</td>' +
        '<td>' + escapeHtml(formatDateTime(row.endDatetime)) + '</td>' +
        '<td>' + escapeHtml(reason) + '</td>' +
        '<td><button type="button" class="button delete-block" data-id="' + escapeHtml(String(row.id)) + '">Ta bort</button></td>' +
      '</tr>';
    }).join('');
  }

  async function fetchBlocks() {
    blocksFeedbackEl.textContent = 'Laddar blockeringar...';
    try {
      const response = await fetch('/api/admin/blocks', { cache: 'no-store' });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data && data.error ? data.error : 'Kunde inte ladda blockeringar');
      }
      state.blocks = Array.isArray(data.blocks) ? data.blocks : [];
      renderBlocks();
      blocksFeedbackEl.textContent = state.blocks.length + ' blockeringar hämtade.';
    } catch (error) {
      blocksFeedbackEl.textContent = 'Fel: ' + error.message;
      blocksRowsEl.innerHTML = '';
    }
  }

  async function createBlock(event) {
    event.preventDefault();
    const payload = {
      trailerType: blockTrailerEl.value,
      startDatetime: blockStartEl.value,
      endDatetime: blockEndEl.value,
      reason: blockReasonEl.value.trim()
    };

    blocksFeedbackEl.textContent = 'Skapar blockering...';
    try {
      const response = await fetch('/api/admin/blocks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data && data.error ? data.error : 'Kunde inte skapa blockering');
      }
      blockFormEl.reset();
      blocksFeedbackEl.textContent = 'Blockering skapad.';
      await fetchBlocks();
    } catch (error) {
      blocksFeedbackEl.textContent = 'Fel: ' + error.message;
    }
  }

  async function deleteBlock(blockId) {
    blocksFeedbackEl.textContent = 'Tar bort blockering...';
    try {
      const response = await fetch('/api/admin/blocks?id=' + encodeURIComponent(blockId), {
        method: 'DELETE'
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data && data.error ? data.error : 'Kunde inte ta bort blockering');
      }
      blocksFeedbackEl.textContent = 'Blockering borttagen.';
      await fetchBlocks();
    } catch (error) {
      blocksFeedbackEl.textContent = 'Fel: ' + error.message;
    }
  }

  function bindEvents() {
    refreshBookingsBtn.addEventListener('click', fetchBookings);
    refreshBlocksBtn.addEventListener('click', fetchBlocks);
    filterDateEl.addEventListener('change', renderBookings);
    filterStatusEl.addEventListener('change', renderBookings);
    filterTrailerEl.addEventListener('change', renderBookings);
    blockFormEl.addEventListener('submit', createBlock);

    blocksRowsEl.addEventListener('click', function (event) {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      if (!target.classList.contains('delete-block')) return;
      const blockId = target.getAttribute('data-id');
      if (!blockId) return;
      deleteBlock(blockId);
    });
  }

  function initDefaults() {
    filterDateEl.value = new Date().toISOString().slice(0, 10);
  }

  async function init() {
    initDefaults();
    bindEvents();
    await fetchBookings();
    await fetchBlocks();
  }

  init();
})();
