import pytest
from banking.face_engine import face_in_guide, FaceCheckError


def test_center_face_ignores_people_outside_guide():
    subject=[230,120,170,240]
    assert face_in_guide([[15,300,75,100],subject,[530,320,60,80]],640,480)==subject


@pytest.mark.parametrize('faces,message',[
    ([], '移到框内'),
    ([[10,100,100,180]],'移到框内'),
    ([[290,220,40,60]],'靠近'),
    ([[150,70,330,340]],'后退'),
    ([[230,120,170,240],[240,130,150,200]],'一张脸'),
    ([[380,120,140,240]],'后退'),
])
def test_outside_small_large_or_ambiguous_faces_do_not_advance(faces,message):
    with pytest.raises(FaceCheckError,match=message):face_in_guide(faces,640,480)


def test_guide_coordinates_follow_frame_aspect_ratio():
    assert face_in_guide([[230,70,170,150]],640,300)==[230,70,170,150]
