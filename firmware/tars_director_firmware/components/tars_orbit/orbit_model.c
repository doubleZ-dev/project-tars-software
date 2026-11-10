#include "orbit_model.h"

typedef struct { const char *name; uint32_t duration; bool action; } entry_t;
static const entry_t catalog[] = {
#define ORBIT_ENTRY(id, name, ms, action) {name, ms, action},
#include "orbit_catalog.def"
#undef ORBIT_ENTRY
};
static int clamp(int n, int lo, int hi) { return n<lo?lo:n>hi?hi:n; }
static int lerp(int a, int b, unsigned q) { return a+(b-a)*(int)q/256; }
static int wave(uint32_t t, unsigned period, int amp) {
    int p=(int)(t%period); return p<(int)period/2 ? -amp+4*amp*p/(int)period : 3*amp-4*amp*p/(int)period;
}
static bool valid(int id) { return id>=0 && id<ORBIT_COUNT; }
const char *orbit_name(orbit_id_t id) { return valid(id)?catalog[id].name:"invalid"; }
uint32_t orbit_duration(orbit_id_t id) { return valid(id)?catalog[id].duration:0; }
bool orbit_is_action(orbit_id_t id) { return valid(id) && catalog[id].action; }
int orbit_find(const char *name) {
    if (!name) return -1;
    for (int i=0;i<ORBIT_COUNT;i++) { const char *a=name,*b=catalog[i].name; while (*a && *a==*b) {a++;b++;} if (!*a && !*b) return i; }
    return -1;
}
void orbit_sample(orbit_id_t id, unsigned eye, uint32_t t, uint32_t duration,
                  unsigned voice, orbit_pose_t *o) {
    const int flip=eye? -1:1;
    const uint32_t d=duration?duration:orbit_duration(id);
    unsigned p=d ? (unsigned)(((uint64_t)(t<d?t:d)*1000)/d) : 0;
    *o=(orbit_pose_t){64,54,60,44,20,0,wave(t,4200,3),0,0,ORBIT_NONE,180,1,0x6DEBDC};
    switch(id) {
    case ORBIT_BOOT:
        o->blink=0; o->gaze=0;
        if(t<450) {o->h=2;o->w=12+(int)t*48/450;o->brow_y=54;}
        else if(t<1100) {o->h=3+(int)(t-450)*12/650;o->decor=ORBIT_SCAN;o->brow_y=35;}
        else if(t<1550) {o->h=15+(int)(t-1100)*29/450;o->brow_y=35-(int)(t-1100)*15/450;}
        else if(t<2250) o->gaze=wave(t-1550,700,10);
        else if(t<2500) {int v=(int)t-2375;if(v<0)v=-v;o->h=3+41*v/125;}
        else {o->curve=14;o->blush=110;}
        break;
    case ORBIT_LISTENING: o->h=52;o->brow_y=12;o->gaze=0;o->decor=ORBIT_VOICE;o->strength=70;break;
    case ORBIT_ANALYZING: case ORBIT_THINKING:
        o->gaze=9;o->y=49;o->h=34;o->tilt=eye?6:-3;o->decor=ORBIT_DOTS;break;
    case ORBIT_SPEAKING: o->h=43+wave(t,560,3);o->brow_y=18+wave(t,1000,2);o->decor=ORBIT_VOICE;o->strength=clamp(voice,0,255);break;
    case ORBIT_EXECUTING: o->h=35;o->brow_y=24;o->tilt=flip*4;o->decor=ORBIT_SCAN;break;
    case ORBIT_WALKING: o->gaze=6;o->y+=wave(t,800,2);o->decor=ORBIT_MOTION;break;
    case ORBIT_CHARGING: o->curve=10;o->decor=ORBIT_BATTERY;o->strength=180;o->blink=0;break;
    case ORBIT_LOW_BATTERY: o->h=18;o->brow_y=31;o->tilt=-flip*5;o->decor=ORBIT_BATTERY;o->strength=35;o->color=0xD6A44A;break;
    case ORBIT_ERROR: o->h=30;o->tilt=-flip*7;o->decor=ORBIT_WARNING;o->color=0xFF8B67;o->gaze=0;break;
    case ORBIT_SLEEP: o->h=3;o->curve=-5;o->brow_y=36;o->decor=ORBIT_ZZZ;o->blink=0;o->color=0x428C87;break;
    case ORBIT_SUPPORT_PROBE: o->h=34;o->gaze=0;o->decor=ORBIT_SCAN;break;
    case ORBIT_HELLO_SMALL: o->h=48;o->brow_y=15;if(p>600)o->curve=9;break;
    case ORBIT_HELLO: o->h=50;o->brow_y=13;o->gaze=wave(t,1500,5);if(p>350){o->curve=14;o->blush=110;}break;
    case ORBIT_BYE: o->curve=10;o->blush=60;o->gaze=wave(t,1800,4);if(p>780){o->h=4;o->curve=-3;}break;
    case ORBIT_RESPOND: o->h=p<350?55:46;o->brow_y=p<350?9:16;o->gaze=0;break;
    case ORBIT_AFFECTION: o->h=53;o->w=64;o->tilt=-flip*7;o->brow_y=14;o->blush=170;o->gaze=wave(t,2400,7);o->decor=ORBIT_HEART;break;
    case ORBIT_THANKS: o->curve=12;o->blush=95;o->y=54+(p<500?(int)p/70:(1000-(int)p)/70);break;
    case ORBIT_HAPPY: o->curve=14;o->blush=130;o->y+=wave(t,750,2);break;
    case ORBIT_EXCITED: o->curve=18;o->blush=180;o->y+=wave(t,440,4);o->brow_y=12;o->decor=ORBIT_STAR;break;
    case ORBIT_ANGRY: o->h=28;o->brow_y=27;o->tilt=flip*9;o->gaze=0;o->y+=wave(t,900,1);o->decor=ORBIT_WARNING;o->strength=80;break;
    case ORBIT_SULKY: o->h=24;o->brow_y=29;o->tilt=eye?4:-1;o->gaze=p>720&&p<860?0:12;break;
    case ORBIT_SAD: o->h=30;o->y=59;o->brow_y=24;o->tilt=-flip*9;o->decor=ORBIT_TEAR;o->gaze=0;break;
    case ORBIT_DISAPPOINTED: o->h=29-(int)p*12/1000;o->y=58;o->brow_y=28;o->tilt=-flip*5;o->gaze=-2;break;
    case ORBIT_SHY: o->h=35;o->gaze=p<250?0:10;o->blush=clamp((int)p/2,0,190);o->tilt=-flip*3;break;
    case ORBIT_SURPRISED: o->h=58;o->w=57;o->brow_y=7;o->gaze=0;o->blink=0;break;
    case ORBIT_NERVOUS: o->h=49;o->w=53;o->brow_y=12;o->tilt=-flip*6;o->gaze=wave(t,600,4);o->decor=ORBIT_SWEAT;break;
    case ORBIT_SLEEPY: o->h=clamp(43-(int)p/24,3,43);if(p>470&&p<600)o->h=32;if(p>850)o->curve=-5;o->brow_y=30;o->decor=ORBIT_ZZZ;break;
    case ORBIT_CURIOUS: o->h=eye?36:51;o->brow_y=eye?23:11;o->tilt=eye?6:-5;o->gaze=p<500?7:-7;break;
    case ORBIT_CONFUSED: {bool other=p>=500;bool high=(eye!=0)^other;o->h=high?50:31;o->brow_y=high?12:26;o->tilt=high?-7:5;o->gaze=other?-5:5;o->decor=ORBIT_QUESTION;break;}
    case ORBIT_PROUD: o->h=eye?24:33;o->tilt=eye?-8:2;o->brow_y=eye?16:25;o->gaze=9;o->decor=ORBIT_STAR;o->strength=100;break;
    case ORBIT_PLAYFUL: o->gaze=p<300?-10:p<600?10:0;if(p>600&&p<780&&eye){o->h=3;o->curve=5;}o->blush=p>800?70:0;break;
    case ORBIT_RELUCTANT: o->h=23;o->gaze=p<600?10:0;o->brow_y=28;o->tilt=eye?-5:2;if(p>650){o->y+=wave((p-650)*5,1750,4);o->h=32;}break;
    default: break;
    }
    /* Deterministic natural blink; special closed-eye gestures keep their own timing. */
    uint32_t b=t%4300;
    if(o->blink && o->curve==0 && b>=3400 && b<3640) {
        int q=(int)b-3520;if(q<0)q=-q;o->h=3+(o->h-3)*q/120;
    }
    o->x+=o->gaze/2;
}
void orbit_blend(const orbit_pose_t *a,const orbit_pose_t *b,unsigned q,orbit_pose_t *o) {
    q=q>256?256:q;q=q*q*(768-2*q)/65536;
    *o=*b;
#define MIX(f) o->f=lerp(a->f,b->f,q)
    MIX(x);MIX(y);MIX(w);MIX(h);MIX(brow_y);MIX(tilt);MIX(gaze);MIX(curve);MIX(blush);MIX(strength);
#undef MIX
}
static void rect(orbit_frame_t *f,int x,int y,int w,int h,int r,int color,int opacity) {
    if(w<=0||h<=0||opacity<=0||f->count>=ORBIT_MAX_PRIMITIVES)return;
    f->items[f->count++]=(orbit_primitive_t){0,x,y,w,h,r,0,color,clamp(opacity,0,255)};
}
static void line(orbit_frame_t *f,int x,int y,int x2,int y2,int width,int color,int opacity) {
    if(opacity<=0||f->count>=ORBIT_MAX_PRIMITIVES)return;
    f->items[f->count++]=(orbit_primitive_t){1,x,y,x2,y2,0,width,color,clamp(opacity,0,255)};
}
void orbit_frame(const orbit_pose_t *p,unsigned eye,uint32_t t,orbit_frame_t *f) {
    f->count=0;int color=p->color;int curve_alpha=clamp((p->curve<0?-p->curve:p->curve)*25,0,255);
    if(p->curve && p->h<=5)curve_alpha=255;
    int eye_alpha=255-curve_alpha;
    rect(f,p->x-p->w/2,p->y-p->h/2,p->w,p->h,p->h/2>19?19:p->h/2,color,eye_alpha);
    if(p->h>16){int pw=p->decor==ORBIT_SWEAT?10:18;
        rect(f,p->x-pw/2+p->gaze/2,p->y-p->h/2+7,pw,p->h-14,7,0x030A0C,eye_alpha);
        rect(f,p->x-p->w/2+10,p->y-p->h/2+7,7,5,2,0xE4FFF6,eye_alpha);}
    line(f,p->x-27,p->brow_y-p->tilt,p->x+27,p->brow_y+p->tilt,5,color,255);
    if(curve_alpha){int oldx=p->x-28,oldy=p->y+p->curve;
        for(int k=1;k<=8;k++){int x=-28+k*7;int y=p->y-p->curve+x*x*p->curve/392;
            line(f,oldx,oldy,p->x+x,y,7,color,curve_alpha);oldx=p->x+x;oldy=y;}}
    for(int k=0;k<4;k++)rect(f,(k<2?24:91)+(k%2)*6,81,3,7,1,0xF28BAF,p->blush);
    int accent=0xF6BE76,x=107,y=75;
    switch(p->decor){
    case ORBIT_STAR: if(eye){int a=150+wave(t,1000,70);line(f,x-6,30,x+6,30,2,accent,a);line(f,x,24,x,36,2,accent,a);line(f,x-3,27,x+3,33,2,accent,a);line(f,x-3,33,x+3,27,2,accent,a);}break;
    case ORBIT_HEART: if(eye){rect(f,100,74,8,8,4,0xF28BAF,200);rect(f,106,74,8,8,4,0xF28BAF,200);for(int i=0;i<5;i++)rect(f,101+i,81+i,12-2*i,1,0,0xF28BAF,200);}break;
    case ORBIT_TEAR: if(!eye){y=69+(int)(t%1800)*18/1800;rect(f,99,y,6,9,3,0x83BEFF,220);line(f,102,y-3,102,y+1,2,0x83BEFF,220);}break;
    case ORBIT_SWEAT: if(eye){rect(f,104,24+wave(t,800,2),6,10,3,0x83BEFF,210);}break;
    case ORBIT_QUESTION: if(eye){line(f,101,24,105,21,2,accent,255);line(f,105,21,111,24,2,accent,255);line(f,111,24,111,29,2,accent,255);line(f,111,29,106,34,2,accent,255);rect(f,105,38,3,3,1,accent,255);}break;
    case ORBIT_DOTS: for(int i=0;i<3;i++)rect(f,49+i*12,85,5,5,2,color,(t/320)%3==(unsigned)i?255:65);break;
    case ORBIT_VOICE: for(int i=0;i<5;i++){int h=3+(p->strength*(5+wave(t+i*120,700,4)))/180;rect(f,47+i*7,88-h/2,3,h,1,color,220);}break;
    case ORBIT_BATTERY: line(f,49,80,49,90,2,color,210);line(f,49,80,75,80,2,color,210);line(f,49,90,75,90,2,color,210);line(f,75,80,75,90,2,color,210);rect(f,77,83,2,4,0,color,210);rect(f,52,83,p->strength<50?4:7+(int)(t%2000)*14/2000,5,1,color,200);break;
    case ORBIT_WARNING: if(eye){line(f,100,79,107,66,2,accent,255);line(f,107,66,114,79,2,accent,255);line(f,114,79,100,79,2,accent,255);rect(f,106,71,2,4,0,accent,255);rect(f,106,77,2,2,0,accent,255);}break;
    case ORBIT_ZZZ: if(eye){int b=wave(t,2800,2);line(f,99,20+b,110,20+b,2,color,180);line(f,110,20+b,99,31+b,2,color,180);line(f,99,31+b,110,31+b,2,color,180);}break;
    case ORBIT_SCAN: line(f,14,32,14,25,1,color,130);line(f,14,25,22,25,1,color,130);line(f,114,70,114,77,1,color,130);line(f,114,77,106,77,1,color,130);rect(f,24,79,10+(int)(t%1600)*70/1600,1,0,color,100);break;
    case ORBIT_MOTION: for(int i=0;i<3;i++){int q=34+i*20;line(f,q,83,q+5,87,2,color,100+(int)((t/250+i)%3)*60);line(f,q+5,87,q,91,2,color,100+(int)((t/250+i)%3)*60);}break;
    default: break;
    }
}
static bool urgent(int id) { return id==ORBIT_ERROR||id==ORBIT_LOW_BATTERY||id==ORBIT_SLEEP||id==ORBIT_BOOT; }
void orbit_control_init(orbit_control_t *c,uint32_t now) { *c=(orbit_control_t){ORBIT_BOOT,-1,now,now,0}; }
bool orbit_control_state(orbit_control_t *c,orbit_id_t state,uint32_t now) {
    if(!valid(state)||orbit_is_action(state))return false;
    if(c->state!=(int)state){c->state=state;c->state_started=now;}
    if(urgent(state))c->action=-1;
    return true;
}
bool orbit_control_action(orbit_control_t *c,orbit_id_t action,uint32_t duration,uint32_t now) {
    if(!orbit_is_action(action)||urgent(c->state))return false;
    c->action=action;c->action_started=now;c->action_duration=duration?duration:orbit_duration(action);
    if(c->action_duration>60000)c->action_duration=60000;
    return true;
}
void orbit_control_cancel(orbit_control_t *c) { c->action=-1; }
orbit_id_t orbit_control_current(orbit_control_t *c,uint32_t now,uint32_t *elapsed,uint32_t *duration) {
    if(c->state==ORBIT_BOOT && now-c->state_started>=orbit_duration(ORBIT_BOOT)){c->state=ORBIT_IDLE;c->state_started+=orbit_duration(ORBIT_BOOT);}
    if(c->action>=0 && now-c->action_started>=c->action_duration)c->action=-1;
    if(c->action>=0){*elapsed=now-c->action_started;*duration=c->action_duration;return c->action;}
    *elapsed=now-c->state_started;*duration=orbit_duration(c->state);return c->state;
}
