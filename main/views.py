import csv

import qrcode.main
from io import BytesIO

from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, get_object_or_404
from django.utils import timezone

from .letter_matrix_generator import generate_letter_matrix
from .models import Session, Round

from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync


def index(request):
    return render(request, 'main/index.html')


def start_session(request):
    if request.method == 'POST':
        session = Session.objects.create(current_round=1)
        web_matrix, mobile_matrix, target_letters = generate_letter_matrix()
        Round.objects.create(
            session=session,
            round_number=1,
            web_matrix=web_matrix,
            mobile_matrix=mobile_matrix,
            target_letters=target_letters
        )
        return render(request, 'main/partials/session_created.html', {
            'session_id': session.session_id
        })

    return HttpResponse('<div class="alert alert-danger">Error</div>', status=405)


def session_view(request, session_id):
    session = get_object_or_404(Session, session_id=session_id)
    current_round = session.rounds.filter(round_number=session.current_round).first()
    web_matrix = current_round.web_matrix
    return render(request, 'main/session.html', {
        'session': session,
        'current_round': current_round,
        'matrix': web_matrix,
        'total_rounds': 10
    })


def select_letter(request, session_id):
    if request.method == 'POST':
        session = get_object_or_404(Session, session_id=session_id)
        current_round = session.rounds.filter(round_number=session.current_round).first()

        letter = request.POST.get('letter')

        web_selection = current_round.web_selection or {'letters': []}
        selected_letters = web_selection.get('letters', [])

        if letter not in selected_letters:
            selected_letters.append(letter)

        current_round.web_selection = {'letters': selected_letters}
        current_round.save()

        target_letters = current_round.target_letters

        correct_selected = [l for l in selected_letters if l in target_letters]

        can_proceed = len(correct_selected) >= 2

        if can_proceed:
            current_round.is_correct = True
            current_round.is_completed = True

            current_round.end_time = timezone.now()
            time_diff = current_round.end_time - current_round.start_time
            current_round.response_time = time_diff.total_seconds()

            current_round.save()

            return render(request, 'main/partials/next_round.html', {
                'session_id': session_id,
                'current_round': session.current_round,
                'total_rounds': 10,
                'can_proceed': True,
                'selected_letters': selected_letters,
                'correct_count': len(correct_selected)
            })
        else:
            return render(request, 'main/partials/next_round.html', {
                'session_id': session_id,
                'current_round': session.current_round,
                'total_rounds': 10,
                'can_proceed': False,
                'selected_letters': selected_letters,
                'correct_count': len(correct_selected),
                'need_more': 2 - len(correct_selected)
            })

    return HttpResponse('<div class="alert alert-danger">Error</div>', status=405)


def next_round(request, session_id):
    if request.method == 'POST':
        session = get_object_or_404(Session, session_id=session_id)

        session.current_round += 1
        session.save()

        if session.current_round > 10:
            session.is_active = False
            session.save()

            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                f'session_{session_id}',
                {
                    'type': 'session_completed'
                }
            )

            return HttpResponse(
                f'<script>window.location.href="/statistics/{session_id}/";</script>'
            )

        web_matrix, mobile_matrix, target_letters = generate_letter_matrix()

        new_round = Round.objects.create(
            session=session,
            round_number=session.current_round,
            web_matrix=web_matrix,
            mobile_matrix=mobile_matrix,
            target_letters=target_letters
        )

        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f'session_{session_id}',
            {
                'type': 'round_updated',
                'current_round': session.current_round,
                'mobile_matrix': mobile_matrix,
                'is_completed': False
            }
        )

        return render(request, 'main/partials/round_content.html', {
            'session': session,
            'round': new_round,
            'matrix': web_matrix,
            'total_rounds': 10
        })

    return HttpResponse('<div class="alert alert-danger">Error</div>', status=405)


def get_statistics(request, session_id):
    session = get_object_or_404(Session, session_id=session_id)
    rounds = session.rounds.all().order_by('round_number')

    completed_rounds = rounds.filter(is_completed=True)

    total_time = sum(r.response_time for r in completed_rounds if r.response_time)
    minutes, seconds = divmod(int(total_time), 60)
    time_to_display = f"{minutes}:{seconds:02}"

    avg_time = total_time / len(completed_rounds) if completed_rounds else 0

    return render(request, 'main/statistics.html', {
        'session': session,
        'rounds': rounds,
        'total_rounds': len(rounds),
        'total_time': time_to_display,
        'average_time': avg_time
    })


def generate_qr(request, session_id):
    host = request.get_host()
    protocol = 'https' if request.is_secure() else 'http'
    mobile_url = f"{protocol}://{host}/mobile/{session_id}/"

    qr = qrcode.main.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(mobile_url)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")

    buffer = BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)

    return HttpResponse(buffer, content_type='image/png')


@csrf_exempt
def api_get_round(request, session_id):
    session = get_object_or_404(Session, session_id=session_id)
    current_round = session.rounds.filter(round_number=session.current_round).first()

    if not current_round:
        return JsonResponse({
            'error': 'Round not found'
        }, status=404)

    return JsonResponse({
        'session_id': str(session.session_id),
        'current_round': session.current_round,
        'total_rounds': 10,
        'is_active': session.is_active,
        'mobile_matrix': current_round.mobile_matrix,
        'target_letters': current_round.target_letters,
        'is_completed': current_round.is_completed
    })


@csrf_exempt
def api_stop_session(request, session_id):
    session = get_object_or_404(Session, session_id=session_id)
    session.is_active = False
    session.save()

    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f'session_{str(session_id)}',
        {
            'type': 'session_stop',
        }
    )
    return HttpResponse(status=204)

def download_csv(request, session_id):
    session = get_object_or_404(Session, session_id=session_id)
    rounds = session.rounds.all().order_by('round_number')

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="session_{session_id}.csv"'

    writer = csv.writer(response)

    writer.writerow([
        'Round Number',
        'Response Time (sec)',
        'Target Letters',
        'Web Selection',
        'Web Matrix',
        'Mobile Matrix'
    ])

    for round_data in rounds:
        writer.writerow([
            round_data.round_number,
            round(round_data.response_time, 1),
            ','.join(round_data.target_letters),
            ','.join(round_data.web_selection.get('letters')),
            ','.join(round_data.web_matrix),
            ','.join(round_data.mobile_matrix)
        ])

    return response