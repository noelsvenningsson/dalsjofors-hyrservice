/*
 * JavaScript for Dalsjöfors Hyrservice booking wizard (Milestone B).
 *
 * This script manages the multi‑step form, communicates with the backend
 * API to fetch live prices and availability, and updates the UI
 * accordingly.  It also provides a dev panel when the query
 * parameter `?dev=1` is present.
 */

document.addEventListener('DOMContentLoaded', () => {
  const qs = new URLSearchParams(window.location.search);
  const devMode = qs.get('dev') === '1';

  // State object to keep selections and API results
  const state = {
    trailerType: null,
    rentalType: null,
    date: null,
    time: null,
    price: null,
    available: null,
    remaining: null,
    remainingByType: { GALLER: null, KAP: null },
  };

  // DOM elements
  const progressEl = document.getElementById('progress');
  const steps = {
    1: document.getElementById('step1'),
    2: document.getElementById('step2'),
    3: document.getElementById('step3'),
    4: document.getElementById('step4'),
  };
  let currentStep = 1;

  // Step 1 elements
  const cardGaller = document.getElementById('card-galler');
  const cardKap = document.getElementById('card-kap');
  const step1Next = document.getElementById('step1-next');

  // Step 2 elements
  const rentalInputs = document.querySelectorAll('input[name="rentalType"]');
  const priceInfo = document.getElementById('price-info');
  const step2Back = document.getElementById('step2-back');
  const step2Next = document.getElementById('step2-next');

  // Step 3 elements
  const dateInput = document.getElementById('rental-date');
  const timeContainer = document.getElementById('time-container');
  const timeSelect = document.getElementById('rental-time');
  const availabilityInfo = document.getElementById('availability-info');
  const step3Back = document.getElementById('step3-back');
  const step3Next = document.getElementById('step3-next');

  // Step 4 elements
  const summaryEl = document.getElementById('summary');
  const step4Back = document.getElementById('step4-back');
  const proceedPay = document.getElementById('proceed-pay');

  // Dev panel
  const devPanel = document.getElementById('dev-panel');
  const debugInfo = document.getElementById('debug-info');
  const devBookBtn = document.getElementById('dev-book');
  if (devMode) {
    devPanel.hidden = false;
  }

  /* Helpers */
  function showStep(n) {
    currentStep = n;
    // Update progress text
    progressEl.textContent = `Steg ${n} av 4`;
    // Show/hide sections
    Object.keys(steps).forEach(key => {
      steps[key].hidden = Number(key) !== n;
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

  function updatePriceInfo() {
    if (!state.rentalType) return;
    // If date is selected, fetch price from API; otherwise show placeholder for full day
    if (state.date) {
      setInfoState(priceInfo, 'Hämtar pris …', 'loading');
      fetch(`/api/price?rentalType=${encodeURIComponent(state.rentalType)}&date=${encodeURIComponent(state.date)}`)
        .then(res => res.json())
        .then(data => {
          if (data && typeof data.price === 'number') {
            state.price = data.price;
            setInfoState(priceInfo, `Pris: ${data.price} kr`, 'success');
          } else {
            setInfoState(priceInfo, 'Kunde inte hämta pris', 'error');
          }
        })
        .catch(() => {
          setInfoState(priceInfo, 'Kunde inte hämta pris', 'error');
        });
    } else {
      // Show generic placeholder for full day
      if (state.rentalType === 'FULL_DAY') {
        setInfoState(priceInfo, 'Pris: 250/300 kr beroende på veckodag', null);
      } else if (state.rentalType === 'TWO_HOURS') {
        setInfoState(priceInfo, 'Pris: 200 kr', null);
      }
    }
  }

  function updateTimeOptions() {
    // Populate time options for 2h rentals (08:00–18:00, 30‑min intervals)
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

  function updateAvailabilityCounts() {
    // Only update if date and rentalType selected
    if (!state.date || !state.rentalType) return;
    if (state.trailerType) {
      setInfoState(availabilityInfo, 'Kontrollerar tillgänglighet …', 'loading');
    }
    // Determine startTime parameter (only for TWO_HOURS)
    const timeParam = state.rentalType === 'TWO_HOURS' && state.time ? `&startTime=${encodeURIComponent(state.time)}` : '';
    ['GALLER', 'KAP'].forEach(type => {
      fetch(`/api/availability?trailerType=${type}&rentalType=${state.rentalType}&date=${state.date}${timeParam}`)
        .then(res => res.json())
        .then(data => {
          state.remainingByType[type] = data.remaining;
          // Update UI for step1 counts
          const el = document.getElementById(`availability-${type.toLowerCase()}`);
          if (el) {
            el.textContent = `${data.remaining}/2 lediga`;
          }
          // If current selected trailer, update availability state and info message
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

  function updateAvailabilityInfo() {
    // Called when availability state updated (for selected trailer)
    if (state.remaining == null) {
      setInfoState(availabilityInfo, '', null);
      return;
    }
    if (state.available) {
      setInfoState(availabilityInfo, `${state.remaining} av 2 lediga`, 'success');
    } else {
      setInfoState(availabilityInfo, 'Fullbokat', 'error');
    }
    // Enable/disable next button
    step3Next.disabled = !state.available;
  }

  function updateSummary() {
    // Compose summary HTML
    if (!state.trailerType || !state.rentalType || !state.date) return;
    const pieces = [];
    pieces.push(`<p><strong>Släp:</strong> ${state.trailerType === 'GALLER' ? 'Gallersläp' : 'Kåpsläp'}</p>`);
    pieces.push(`<p><strong>Datum:</strong> ${state.date}</p>`);
    if (state.rentalType === 'TWO_HOURS') {
      pieces.push(`<p><strong>Tid:</strong> ${state.time} – ${calcEndTime(state.time)}</p>`);
      pieces.push(`<p><strong>Längd:</strong> 2 timmar</p>`);
    } else {
      pieces.push(`<p><strong>Längd:</strong> Heldag</p>`);
    }
    if (state.price != null) {
      pieces.push(`<p><strong>Pris:</strong> ${state.price} kr</p>`);
    }
    summaryEl.innerHTML = pieces.join('');
  }

  function calcEndTime(startTime) {
    if (!startTime) return '';
    const [h, m] = startTime.split(':').map(n => parseInt(n, 10));
    let endH = h + 2;
    let endM = m;
    if (endH >= 24) endH -= 24;
    return `${endH.toString().padStart(2, '0')}:${endM.toString().padStart(2, '0')}`;
  }

  function updateDebugInfo() {
    if (!devMode) return;
    // Show current start/end, remaining and selection
    let debug = '';
    if (state.date) {
      const startDT = state.date + (state.rentalType === 'TWO_HOURS' && state.time ? `T${state.time}` : 'T00:00');
      let endDT;
      if (state.rentalType === 'TWO_HOURS' && state.time) {
        endDT = state.date + 'T' + calcEndTime(state.time);
      } else if (state.rentalType === 'FULL_DAY') {
        endDT = state.date + 'T23:59';
      }
      debug += `<p><strong>start_dt:</strong> ${startDT}</p>`;
      debug += `<p><strong>end_dt:</strong> ${endDT || ''}</p>`;
    }
    if (state.remaining != null) {
      debug += `<p><strong>remaining:</strong> ${state.remaining}</p>`;
    }
    debugInfo.innerHTML = debug;
  }

  /* Event listeners */
  // Step 1: select trailer type
  [cardGaller, cardKap].forEach(card => {
    card.addEventListener('click', () => {
      // Set selected
      [cardGaller, cardKap].forEach(c => c.classList.remove('selected'));
      card.classList.add('selected');
      state.trailerType = card.dataset.type;
      step1Next.disabled = false;
      // If date/time already selected, update availability for this selection
      updateAvailabilityCounts();
    });
  });

  step1Next.addEventListener('click', () => {
    showStep(2);
  });

  // Step 2: rental type selection
  rentalInputs.forEach(radio => {
    radio.addEventListener('change', () => {
      state.rentalType = radio.value;
      // Show/hide time select on step3 accordingly
      timeContainer.hidden = state.rentalType !== 'TWO_HOURS';
      if (state.rentalType === 'TWO_HOURS') {
        updateTimeOptions();
      }
      updatePriceInfo();
      // Enable next
      step2Next.disabled = false;

      // If a date is already selected and possibly a time (for two hours),
      // refresh availability counts so that step1 shows live remaining when
      // the rental type changes.
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

  // Step 3: date/time selection
  dateInput.addEventListener('change', () => {
    state.date = dateInput.value;
    // Price may depend on date
    updatePriceInfo();
    // Reset time selection when date changes
    state.time = state.rentalType === 'TWO_HOURS' ? timeSelect.value : null;
    // After selecting date/time, update availability counts
    updateAvailabilityCounts();
    // Validate step3 next button
    if (state.rentalType === 'FULL_DAY' && state.date) {
      // For full day, we don't need time selection
      // step3Next disabled until availability call returns
      step3Next.disabled = true;
    } else if (state.rentalType === 'TWO_HOURS' && state.date && state.time) {
      step3Next.disabled = true;
    }
  });

  timeSelect.addEventListener('change', () => {
    state.time = timeSelect.value;
    updateAvailabilityCounts();
    step3Next.disabled = true; // wait until availability call updates available flag
  });

  step3Back.addEventListener('click', () => {
    showStep(2);
  });
  step3Next.addEventListener('click', () => {
    updateSummary();
    showStep(4);
  });

  // Step 4
  step4Back.addEventListener('click', () => {
    showStep(3);
  });
  proceedPay.addEventListener('click', () => {
    // Create booking hold via API and then redirect to the payment page.
    const defaultLabel = proceedPay.textContent;
    proceedPay.textContent = 'Reserverar …';
    setButtonLoading(proceedPay, true);
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
          // Redirect to payment page with bookingId
          window.location.href = `/pay?bookingId=${encodeURIComponent(data.bookingId)}`;
        } else if (data && data.error) {
          proceedPay.textContent = defaultLabel;
          setButtonLoading(proceedPay, false);
          alert(`Kunde inte reservera bokning: ${data.error}`);
        } else {
          proceedPay.textContent = defaultLabel;
          setButtonLoading(proceedPay, false);
        }
      })
      .catch(() => {
        proceedPay.textContent = defaultLabel;
        setButtonLoading(proceedPay, false);
        alert('Fel vid kontakt med servern');
      });
  });

  // Dev panel actions
  devBookBtn.addEventListener('click', () => {
    // Create hold booking using current selections for quick testing
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
          // refresh counts after booking
          updateAvailabilityCounts();
        } else if (data && data.error) {
          alert(`Fel: ${data.error}`);
        }
      })
      .catch(() => alert('Fel vid dev‑bokning'));
  });

  // Kick off by showing step1
  showStep(1);
});
