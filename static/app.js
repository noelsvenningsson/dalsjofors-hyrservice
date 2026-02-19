/*
 * JavaScript for Dalsjöfors Hyrservice booking wizard.
 */

document.addEventListener('DOMContentLoaded', () => {
  const qs = new URLSearchParams(window.location.search);
  const devMode = qs.get('dev') === '1';

  const state = {
    trailerType: null,
    rentalType: null,
    date: null,
    time: null,
    customerPhone: null,
    price: null,
    dayTypeLabel: null,
    available: null,
    remaining: null,
    remainingByType: { GALLER: null, KAP: null },
    bookingId: null,
    bookingReference: null,
    createdAt: null,
    swishStatus: null,
    pollTimer: null,
  };

  const progressEl = document.getElementById('progress');
  const steps = {
    1: document.getElementById('step1'),
    2: document.getElementById('step2'),
    3: document.getElementById('step3'),
    4: document.getElementById('step4'),
    5: document.getElementById('step5'),
  };
  let currentStep = 1;

  const cardGaller = document.getElementById('card-galler');
  const cardKap = document.getElementById('card-kap');
  const step1Next = document.getElementById('step1-next');

  const rentalInputs = document.querySelectorAll('input[name="rentalType"]');
  const priceInfo = document.getElementById('price-info');
  const dayTypeBadge = document.getElementById('daytype-badge');
  const step2Back = document.getElementById('step2-back');
  const step2Next = document.getElementById('step2-next');

  const dateInput = document.getElementById('rental-date');
  const timeContainer = document.getElementById('time-container');
  const timeSelect = document.getElementById('rental-time');
  const customerPhoneInput = document.getElementById('customer-phone');
  const availabilityInfo = document.getElementById('availability-info');
  const step3Back = document.getElementById('step3-back');
  const step3Next = document.getElementById('step3-next');

  const summaryEl = document.getElementById('summary');
  const step4Back = document.getElementById('step4-back');
  const paymentPanel = document.getElementById('payment-panel');
  const paymentInfo = document.getElementById('payment-info');
  const qrWrap = document.getElementById('qr-wrap');
  const paymentQr = document.getElementById('payment-qr');
  const openSwishLink = document.getElementById('open-swish-link');
  const retryPayment = document.getElementById('retry-payment');

  const confirmationEl = document.getElementById('confirmation');

  const devPanel = document.getElementById('dev-panel');
  const debugInfo = document.getElementById('debug-info');
  const devBookBtn = document.getElementById('dev-book');
  if (devMode) {
    devPanel.hidden = false;
  }

  function showStep(n) {
    currentStep = n;
    progressEl.textContent = `Steg ${n} av 5`;
    Object.keys(steps).forEach(key => {
      const stepNode = steps[key];
      const isTarget = Number(key) === n;
      if (isTarget) {
        stepNode.hidden = false;
        requestAnimationFrame(() => stepNode.classList.add('is-visible'));
      } else {
        stepNode.classList.remove('is-visible');
        stepNode.hidden = true;
      }
    });
  }

  function setInfoState(el, text, stateClass) {
    el.textContent = text;
    el.classList.remove('loading', 'error', 'success', 'warning');
    if (stateClass) {
      el.classList.add(stateClass);
    }
  }

  function setButtonLoading(button, loading) {
    button.disabled = loading;
    button.classList.toggle('is-loading', loading);
  }

  function stopPaymentPolling() {
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  function isPaid(status) {
    return status === 'PAID';
  }

  function isFailed(status) {
    return status === 'FAILED';
  }

  function readSwishStatus(data, sourceLabel) {
    if (data && typeof data.swishStatus === 'string') {
      return data.swishStatus;
    }
    console.warn(`[payment] Missing swishStatus from ${sourceLabel}. Defaulting to PENDING.`, data);
    return 'PENDING';
  }

  function gotoConfirmation() {
    if (!isPaid(state.swishStatus)) {
      console.warn('[payment] Confirmation blocked because swishStatus is not PAID.', state.swishStatus);
      return;
    }
    renderConfirmation();
    showStep(5);
  }

  function updatePriceInfo() {
    if (!state.rentalType) return;
    if (state.date) {
      setInfoState(priceInfo, 'Hämtar pris …', 'loading');
      fetch(`/api/price?trailerType=${encodeURIComponent(state.trailerType || 'GALLER')}&rentalType=${encodeURIComponent(state.rentalType)}&date=${encodeURIComponent(state.date)}`)
        .then(res => res.json())
        .then(data => {
          if (data && typeof data.price === 'number') {
            state.price = data.price;
            state.dayTypeLabel = data.dayTypeLabel || null;
            const daySuffix = state.dayTypeLabel ? ` (${state.dayTypeLabel})` : '';
            setInfoState(priceInfo, `Pris: ${data.price} kr${daySuffix}`, 'success');
            if (state.rentalType === 'FULL_DAY' && state.dayTypeLabel) {
              dayTypeBadge.hidden = false;
              dayTypeBadge.textContent = state.dayTypeLabel;
            } else {
              dayTypeBadge.hidden = true;
              dayTypeBadge.textContent = '';
            }
          } else {
            setInfoState(priceInfo, 'Kunde inte hämta pris', 'error');
          }
        })
        .catch(() => {
          setInfoState(priceInfo, 'Kunde inte hämta pris', 'error');
        });
    } else {
      if (state.rentalType === 'FULL_DAY') {
        setInfoState(priceInfo, 'Välj datum för exakt heldagspris', null);
        dayTypeBadge.hidden = true;
        dayTypeBadge.textContent = '';
      } else if (state.rentalType === 'TWO_HOURS') {
        setInfoState(priceInfo, 'Pris: 200 kr', null);
        dayTypeBadge.hidden = true;
        dayTypeBadge.textContent = '';
      }
    }
  }

  function updateTimeOptions() {
    timeSelect.innerHTML = '';
    const startHour = 8;
    const endHour = 18;
    for (let h = startHour; h <= endHour; h++) {
      for (let m = 0; m < 60; m += 30) {
        const hourStr = h.toString().padStart(2, '0');
        const minuteStr = m.toString().padStart(2, '0');
        const value = `${hourStr}:${minuteStr}`;
        const opt = document.createElement('option');
        opt.value = value;
        opt.textContent = value;
        timeSelect.appendChild(opt);
      }
    }
  }

  function updateAvailabilityInfo() {
    if (state.remaining == null) {
      setInfoState(availabilityInfo, '', null);
      return;
    }
    if (state.available) {
      setInfoState(availabilityInfo, `${state.remaining} av 2 lediga`, 'success');
    } else {
      setInfoState(availabilityInfo, 'Fullbokat', 'error');
    }
    step3Next.disabled = !state.available;
  }

  function updateAvailabilityCounts() {
    if (!state.date || !state.rentalType) return;
    if (state.trailerType) {
      setInfoState(availabilityInfo, 'Kontrollerar tillgänglighet …', 'loading');
    }
    const timeParam = state.rentalType === 'TWO_HOURS' && state.time ? `&startTime=${encodeURIComponent(state.time)}` : '';
    ['GALLER', 'KAP'].forEach(type => {
      fetch(`/api/availability?trailerType=${type}&rentalType=${state.rentalType}&date=${state.date}${timeParam}`)
        .then(res => res.json())
        .then(data => {
          state.remainingByType[type] = data.remaining;
          const el = document.getElementById(`availability-${type.toLowerCase()}`);
          if (el) {
            el.textContent = `${data.remaining}/2 lediga`;
          }
          if (state.trailerType === type) {
            state.available = data.available;
            state.remaining = data.remaining;
            updateAvailabilityInfo();
          }
          updateDebugInfo();
        })
        .catch(() => {
          if (state.trailerType === type) {
            setInfoState(availabilityInfo, 'Kunde inte läsa tillgänglighet', 'error');
          }
        });
    });
  }

  function calcEndTime(startTime) {
    if (!startTime) return '';
    const [h, m] = startTime.split(':').map(n => parseInt(n, 10));
    let endH = h + 2;
    if (endH >= 24) endH -= 24;
    return `${endH.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}`;
  }

  function updateSummary() {
    if (!state.trailerType || !state.rentalType || !state.date) return;
    const pieces = [];
    pieces.push(`<p><strong>Släp:</strong> ${state.trailerType === 'GALLER' ? 'Galler-släp' : 'Kåpsläp'}</p>`);
    pieces.push(`<p><strong>Datum:</strong> ${state.date}</p>`);
    if (state.rentalType === 'TWO_HOURS') {
      pieces.push(`<p><strong>Tid:</strong> ${state.time} - ${calcEndTime(state.time)}</p>`);
      pieces.push('<p><strong>Längd:</strong> 2 timmar</p>');
    } else {
      pieces.push('<p><strong>Längd:</strong> Heldag</p>');
    }
    if (state.price != null) {
      const daySuffix = state.dayTypeLabel ? ` (${state.dayTypeLabel})` : '';
      pieces.push(`<p><strong>Pris:</strong> ${state.price} kr${daySuffix}</p>`);
    }
    if (state.customerPhone) {
      pieces.push('<p><strong>Mobil för kvitto:</strong> Angivet</p>');
    }
    if (state.bookingReference) {
      pieces.push(`<p><strong>Bokningsreferens:</strong> ${state.bookingReference}</p>`);
    }
    summaryEl.innerHTML = pieces.join('');
  }

  function renderConfirmation() {
    const trailerText = state.trailerType === 'GALLER' ? 'Galler-släp' : 'Kåpsläp';
    const nowIso = new Date().toISOString().slice(0, 19).replace('T', ' ');
    const rows = [];
    if (state.bookingReference) {
      rows.push(`<p><strong>Referens:</strong> ${state.bookingReference}</p>`);
    }
    rows.push(`<p><strong>Datum/tid:</strong> ${state.date}${state.rentalType === 'TWO_HOURS' ? ` ${state.time}-${calcEndTime(state.time)}` : ' Heldag'}</p>`);
    rows.push(`<p><strong>Släp:</strong> ${trailerText}</p>`);
    if (state.price != null) {
      rows.push(`<p><strong>Pris:</strong> ${state.price} kr</p>`);
    }
    rows.push(`<p><strong>Betalstatus:</strong> ${state.swishStatus || 'PAID'}</p>`);
    rows.push(`<p><strong>Skapad:</strong> ${state.createdAt || nowIso}</p>`);
    confirmationEl.innerHTML = rows.join('');
  }

  function updateDebugInfo() {
    if (!devMode) return;
    const bits = [];
    if (state.date) {
      const startDT = state.date + (state.rentalType === 'TWO_HOURS' && state.time ? `T${state.time}` : 'T00:00');
      let endDT = '';
      if (state.rentalType === 'TWO_HOURS' && state.time) {
        endDT = state.date + 'T' + calcEndTime(state.time);
      } else if (state.rentalType === 'FULL_DAY') {
        endDT = state.date + 'T23:59';
      }
      bits.push(`<p><strong>start_dt:</strong> ${startDT}</p>`);
      bits.push(`<p><strong>end_dt:</strong> ${endDT}</p>`);
    }
    if (state.bookingId) {
      bits.push(`<p><strong>booking_id:</strong> ${state.bookingId}</p>`);
    }
    if (state.swishStatus) {
      bits.push(`<p><strong>swish_status:</strong> ${state.swishStatus}</p>`);
    }
    debugInfo.innerHTML = bits.join('');
  }

  function createBookingHold() {
    if (state.bookingId) {
      return Promise.resolve(state.bookingId);
    }
    const payload = {
      trailerType: state.trailerType,
      rentalType: state.rentalType,
      date: state.date,
    };
    if (state.rentalType === 'TWO_HOURS') payload.startTime = state.time;
    if (state.customerPhone) payload.customerPhone = state.customerPhone;

    return fetch('/api/hold', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
      .then(res => res.json().then(data => ({ ok: res.ok, data })))
      .then(({ ok, data }) => {
        if (!ok || !data || !data.bookingId) {
          throw new Error(data?.errorInfo?.message || data?.error || 'Kunde inte skapa bokning');
        }
        state.bookingId = data.bookingId;
        state.bookingReference = data.bookingReference || null;
        state.createdAt = data.createdAt || null;
        updateSummary();
        updateDebugInfo();
        return state.bookingId;
      });
  }

  function requestSwishPayment() {
    if (!state.bookingId) {
      return Promise.reject(new Error('bookingId saknas'));
    }
    setInfoState(paymentInfo, 'Skapar betalningsförfrågan …', 'loading');
    paymentPanel.hidden = false;
    retryPayment.hidden = true;

    return fetch(`/api/swish/paymentrequest?bookingId=${encodeURIComponent(state.bookingId)}`, {
      method: 'POST',
    })
      .then(res => res.json().then(data => ({ ok: res.ok, data })))
      .then(({ ok, data }) => {
        if (!ok) {
          throw new Error(data?.errorInfo?.message || data?.error || 'Betalning kunde inte startas');
        }

        const swishStatus = readSwishStatus(data, '/api/swish/paymentrequest');
        state.swishStatus = swishStatus;
        updateDebugInfo();
        if (data.qrUrl) {
          qrWrap.hidden = false;
          paymentQr.src = data.qrUrl;
        }
        if (data.swishAppUrl) {
          openSwishLink.hidden = false;
          openSwishLink.href = data.swishAppUrl;
        } else {
          openSwishLink.hidden = true;
          openSwishLink.removeAttribute('href');
        }

        // Confirmation must only depend on swishStatus (never on generic data.status).
        if (isPaid(swishStatus)) {
          setInfoState(paymentInfo, 'Betalning registrerad.', 'success');
          stopPaymentPolling();
          gotoConfirmation();
          return;
        }

        if (isFailed(swishStatus)) {
          stopPaymentPolling();
          setInfoState(paymentInfo, 'Betalningen misslyckades. Försök igen.', 'error');
          retryPayment.hidden = false;
          return;
        }

        setInfoState(paymentInfo, data.idempotent ? 'Väntar på betalning... Återanvänder befintlig förfrågan.' : 'Väntar på betalning...', 'loading');
        startPaymentPolling();
      });
  }

  function startPaymentPolling() {
    stopPaymentPolling();
    state.pollTimer = setInterval(() => {
      if (!state.bookingId || currentStep !== 4) return;
      fetch(`/api/payment-status?bookingId=${encodeURIComponent(state.bookingId)}`)
        .then(res => res.json().then(data => ({ ok: res.ok, data })))
        .then(({ ok, data }) => {
          if (!ok) return;
          const swishStatus = readSwishStatus(data, '/api/payment-status');
          state.swishStatus = swishStatus;
          updateDebugInfo();
          if (isPaid(swishStatus)) {
            stopPaymentPolling();
            setInfoState(paymentInfo, 'Betalning registrerad.', 'success');
            gotoConfirmation();
          } else if (isFailed(swishStatus)) {
            stopPaymentPolling();
            setInfoState(paymentInfo, 'Betalningen misslyckades. Försök igen.', 'error');
            retryPayment.hidden = false;
          } else {
            setInfoState(paymentInfo, 'Väntar på betalning...', 'loading');
          }
        })
        .catch(() => {
          // Keep polling on transient failures.
        });
    }, 2500);
  }

  function startStep4PaymentFlow() {
    paymentPanel.hidden = false;
    setInfoState(paymentInfo, 'Startar betalningsflöde …', 'loading');
    qrWrap.hidden = true;
    paymentQr.removeAttribute('src');
    openSwishLink.hidden = true;
    retryPayment.hidden = true;

    createBookingHold()
      .then(() => requestSwishPayment())
      .catch(err => {
        setInfoState(paymentInfo, err.message || 'Kunde inte starta betalning', 'error');
        retryPayment.hidden = false;
      });
  }

  [cardGaller, cardKap].forEach(card => {
    card.addEventListener('click', () => {
      [cardGaller, cardKap].forEach(c => c.classList.remove('selected'));
      card.classList.add('selected');
      state.trailerType = card.dataset.type;
      step1Next.disabled = false;
      updateAvailabilityCounts();
      updatePriceInfo();
    });
  });

  step1Next.addEventListener('click', () => {
    showStep(2);
  });

  rentalInputs.forEach(radio => {
    radio.addEventListener('change', () => {
      state.rentalType = radio.value;
      timeContainer.hidden = state.rentalType !== 'TWO_HOURS';
      if (state.rentalType === 'TWO_HOURS') {
        updateTimeOptions();
        state.time = timeSelect.value;
      } else {
        state.time = null;
      }
      updatePriceInfo();
      step2Next.disabled = false;
      if (state.date) {
        updateAvailabilityCounts();
      }
    });
  });

  step2Back.addEventListener('click', () => {
    showStep(1);
  });

  step2Next.addEventListener('click', () => {
    showStep(3);
  });

  dateInput.addEventListener('change', () => {
    state.date = dateInput.value;
    updatePriceInfo();
    if (state.rentalType === 'TWO_HOURS') {
      state.time = timeSelect.value;
    } else {
      state.time = null;
    }
    updateAvailabilityCounts();
    step3Next.disabled = true;
  });

  timeSelect.addEventListener('change', () => {
    state.time = timeSelect.value;
    updateAvailabilityCounts();
    step3Next.disabled = true;
  });

  step3Back.addEventListener('click', () => {
    showStep(2);
  });

  step3Next.addEventListener('click', () => {
    state.customerPhone = (customerPhoneInput.value || '').trim() || null;
    updateSummary();
    showStep(4);
    startStep4PaymentFlow();
  });

  step4Back.addEventListener('click', () => {
    stopPaymentPolling();
    showStep(3);
  });

  retryPayment.addEventListener('click', () => {
    requestSwishPayment().catch(err => {
      setInfoState(paymentInfo, err.message || 'Kunde inte skapa betalning', 'error');
      retryPayment.hidden = false;
    });
  });

  devBookBtn.addEventListener('click', () => {
    if (!state.trailerType || !state.rentalType || !state.date) {
      alert('Välj släp, hyrestid och datum först');
      return;
    }
    const payload = {
      trailerType: state.trailerType,
      rentalType: state.rentalType,
      date: state.date,
    };
    if (state.rentalType === 'TWO_HOURS') payload.startTime = state.time;
    fetch('/api/hold', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
      .then(res => res.json())
      .then(data => {
        if (data && data.bookingId) {
          alert(`[dev] Bokning skapad med id ${data.bookingId}`);
          updateAvailabilityCounts();
        } else if (data && data.error) {
          alert(`Fel: ${data.error}`);
        }
      })
      .catch(() => alert('Fel vid dev-bokning'));
  });

  showStep(1);
});
